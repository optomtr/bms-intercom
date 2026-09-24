"""Голос оператора → терминал через HCNetSDK: помощник `bms_talk` подпроцессом.

Зачем: DS-K1T341AM не принимает звук по ISAPI (404 notSupport), а по HCNetSDK
(порт 8000) принимает — проверено на живом терминале. HCNetSDK — это glibc-
библиотеки Hikvision; HA Core живёт на Alpine (musl), поэтому помощник лежит
в интеграции вместе со своими .so и glibc-загрузчиком (`sdk/<arch>/`) и
запускается ТОЛЬКО через загрузчик — напрямую musl его не исполнит.

Протокол (stdin/stdout помощника):
- stdin, первая строка: JSON {host, port, user, password, channel}. Пароль —
  только так: argv и окружение видны в `ps` любому процессу хоста;
- stdout, одна строка: {"type":"started","codec":…} или
  {"type":"error","code":N,"message":…} (после error помощник выходит);
- дальше в stdin — сырой G.711 µ-law 8 кГц любыми кусками (нарезает помощник);
  закрытый stdin = «положить трубку», помощник выходит сам;
- stderr — его журнал, перекладываем в наш DEBUG построчно;
- `--selftest` → {"type":"selftest","sdk":"…"}: помощник вообще запускается.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import time

_LOGGER = logging.getLogger(__name__)

SDK_DIR = os.path.join(os.path.dirname(__file__), "sdk")
SDK_PORT = 8000
SDK_CHANNEL = 1
_LOADERS = {"aarch64": "ld-linux-aarch64.so.1", "amd64": "ld-linux-x86-64.so.2"}

SELFTEST_TIMEOUT = 10
#: Вход в SDK + открытие голоса: терминал под нагрузкой отвечает не мгновенно.
START_TIMEOUT = 15
#: Сколько ждём, что помощник выйдет сам после закрытия stdin, до terminate/kill.
STOP_TIMEOUT = 3
#: Неудачный selftest повторяем не чаще — иначе каждый talk_start ждал бы 10 с.
RETRY_FAILED_AFTER = 300

NO_PLATFORM = "голос по SDK недоступен на этой платформе"

#: Коды HCNetSDK (NET_DVR_GetLastError) при входе — словами для владельца.
_SDK_ERRORS = {
    1: "неверный логин или пароль терминала",
    7: "терминал не отвечает на порту SDK",
    10: "терминал не ответил вовремя",
    153: "учётная запись терминала заблокирована после неверных паролей",
}


class SDKAudioError(Exception):
    """Голос по SDK не открылся или оборвался; текст — для журнала и атрибута."""


def describe_error(code: object, message: object) -> str:
    """Ответ помощника {"type":"error"} → понятная фраза (код — для поддержки)."""
    text = _SDK_ERRORS.get(code) if isinstance(code, int) else None
    return f"голос по SDK: {text or message or 'неизвестная ошибка'} (код {code})"


def _arch() -> str | None:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "amd64"
    return None


def _prepare_helper() -> tuple[list[str], str] | str:
    """(команда, рабочая папка) помощника — или причина, почему его нет.

    Блокирующий ввод-вывод: только в executor. HACS при установке теряет
    exec-бит, поэтому ставим его сами перед запуском.
    """
    arch = _arch()
    folder = os.path.join(SDK_DIR, arch) if arch else ""
    loader = os.path.join(folder, _LOADERS.get(arch or "", ""))
    helper = os.path.join(folder, "bms_talk")
    if not arch or not (os.path.isfile(loader) and os.path.isfile(helper)):
        return NO_PLATFORM
    for path in (loader, helper):
        if not os.access(path, os.X_OK):
            try:
                os.chmod(path, 0o755)
            except OSError as err:
                return f"помощник голоса без права запуска: {err}"
    lib = f"{folder}:{folder}/HCNetSDKCom"
    return [loader, "--library-path", lib, helper], folder


async def _async_helper() -> tuple[list[str], str] | str:
    return await asyncio.get_running_loop().run_in_executor(None, _prepare_helper)


# Один помощник на весь HA: вердикт общий для всех домофонов.
_CHECK: dict[str, object] = {}


def reset_cache() -> None:
    """Забыть вердикт selftest (тесты; после обновления файлов помощника)."""
    _CHECK.clear()


async def async_check() -> tuple[bool, str]:
    """(работает ли помощник, пояснение). Успех кешируется навсегда.

    Нет папки под архитектуру — это навсегда (до обновления интеграции, а оно
    перезапускает HA); сбой selftest — повод повторить через RETRY_FAILED_AFTER.
    """
    if "until" in _CHECK and time.monotonic() < float(_CHECK["until"]):  # type: ignore[arg-type]
        return bool(_CHECK["ok"]), str(_CHECK["why"])
    ok, why, forever = await _async_selftest()
    _CHECK.update(ok=ok, why=why, until=float("inf") if forever else
                  time.monotonic() + RETRY_FAILED_AFTER)
    return ok, why


async def _async_selftest() -> tuple[bool, str, bool]:
    helper = await _async_helper()
    if isinstance(helper, str):
        return False, helper, helper == NO_PLATFORM
    cmd, cwd = helper
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, "--selftest", cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        return False, f"помощник голоса не запустился: {err}", False
    try:
        out, err_out = await asyncio.wait_for(proc.communicate(), SELFTEST_TIMEOUT)
    except TimeoutError:
        await _async_kill(proc)
        return False, "помощник голоса не ответил на самопроверку", False
    for line in err_out.decode(errors="replace").splitlines():
        _LOGGER.debug("[bms_talk selftest] %s", line)
    reply = _parse_line(out.split(b"\n", 1)[0])
    if reply.get("type") != "selftest":
        return False, f"помощник голоса не прошёл самопроверку (код выхода {proc.returncode})", False
    _LOGGER.debug("Помощник голоса SDK готов: HCNetSDK %s", reply.get("sdk"))
    return True, f"HCNetSDK {reply.get('sdk') or '?'}", True


def _parse_line(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode(errors="replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _async_kill(proc: asyncio.subprocess.Process) -> None:
    """terminate → kill: зависший помощник не должен пережить разговор."""
    for stop in (proc.terminate, proc.kill):
        try:
            stop()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 2)
            return
        except TimeoutError:
            continue


class SDKTalkSession:
    """Один разговор = один процесс помощника; интерфейс как у ISAPI-сессии."""

    def __init__(self) -> None:
        self.codec = "G.711ulaw"
        self._proc: asyncio.subprocess.Process | None = None
        self._pumps: list[asyncio.Task] = []
        self._secret = ""
        self._lock = asyncio.Lock()

    async def async_open(self, host: str, port: int, user: str, password: str) -> str:
        """Запустить помощника, войти на терминал; вернуть кодек или SDKAudioError."""
        helper = await _async_helper()
        if isinstance(helper, str):
            raise SDKAudioError(helper)
        cmd, cwd = helper
        self._secret = password
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            raise SDKAudioError(f"помощник голоса не запустился: {err}") from err
        self._proc = proc
        self._pumps.append(asyncio.create_task(self._pump_stderr(proc)))
        start = {"host": host, "port": port, "user": user,
                 "password": password, "channel": SDK_CHANNEL}
        try:
            proc.stdin.write(json.dumps(start).encode() + b"\n")
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), START_TIMEOUT)
        except TimeoutError:
            await self.async_close()
            raise SDKAudioError("голос по SDK: терминал не ответил за %d с" % START_TIMEOUT) from None
        except (BrokenPipeError, ConnectionResetError) as err:
            await self.async_close()
            raise SDKAudioError("помощник голоса завершился при запуске") from err
        reply = _parse_line(line)
        if reply.get("type") == "started":
            self.codec = str(reply.get("codec") or self.codec)
            self._pumps.append(asyncio.create_task(self._pump_stdout(proc)))
            return self.codec
        await self.async_close()
        if reply.get("type") == "error":
            raise SDKAudioError(describe_error(reply.get("code"), reply.get("message")))
        raise SDKAudioError(
            f"помощник голоса завершился без ответа (код выхода {proc.returncode})"
        )

    def _clean(self, text: str) -> str:
        # Помощник чужой: если он напишет пароль в свой журнал, в журнал HA
        # пароль всё равно не попадёт.
        return text.replace(self._secret, "***") if self._secret else text

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        async for raw in proc.stderr:
            _LOGGER.debug("[bms_talk] %s", self._clean(raw.decode(errors="replace").rstrip()))

    async def _pump_stdout(self, proc: asyncio.subprocess.Process) -> None:
        # После started помощник молчит; читаем всё равно, чтобы полная труба
        # не остановила его, а поздний error попал в журнал.
        async for raw in proc.stdout:
            reply = _parse_line(raw)
            if reply.get("type") == "error":
                _LOGGER.warning("%s", describe_error(reply.get("code"), reply.get("message")))
            else:
                _LOGGER.debug("[bms_talk] %s", self._clean(raw.decode(errors="replace").rstrip()))

    async def async_send(self, data: bytes) -> None:
        """Отдать кусок G.711 µ-law помощнику."""
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is not None:
            raise SDKAudioError(f"помощник голоса завершился (код выхода {proc.returncode})")
        async with self._lock:
            try:
                proc.stdin.write(data)
                await asyncio.wait_for(proc.stdin.drain(), 5)
            except (BrokenPipeError, ConnectionResetError, TimeoutError) as err:
                raise SDKAudioError(f"помощник голоса не принимает звук: {err!r}") from err

    async def async_close(self) -> None:
        """Закрыть stdin (помощник кладёт трубку и выходит), зависшего — снять."""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
            except TimeoutError:
                await _async_kill(proc)
        pumps, self._pumps = self._pumps, []
        if pumps:
            # Процесс вышел — трубы закрыты; даём дочитать последние строки журнала.
            await asyncio.wait(pumps, timeout=1)
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
