"""Голос оператора → терминал через HCNetSDK: помощник `bms_talk` и его проверка.

Зачем: DS-K1T341AM не принимает звук по ISAPI (404 notSupport), а по HCNetSDK
(порт 8000) принимает — проверено на живом терминале. HCNetSDK — это glibc-
библиотеки Hikvision; HA Core живёт на Alpine (musl), поэтому помощник лежит
в интеграции вместе со своими .so и glibc-загрузчиком (`sdk/<arch>/`) и
запускается ТОЛЬКО через загрузчик — напрямую musl его не исполнит.

Здесь — общее: где помощник, `--selftest` (запускается ли он вообще на этой
платформе), коды ошибок SDK словами. Сам разговор с 0.3.8 идёт через
постоянный помощник `--serve` (sdkhelper.py): вход на терминал один раз при
загрузке интеграции, а не на каждый «Ответить».

- `--selftest` → {"type":"selftest","sdk":"…"}: помощник вообще запускается;
- пароль помощнику — только первой строкой stdin: argv и окружение видны в
  `ps` любому процессу хоста;
- stderr помощника — его журнал, перекладываем в наш DEBUG построчно.
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
#: Неудачный selftest повторяем не чаще — иначе каждый talk_start ждал бы 10 с.
RETRY_FAILED_AFTER = 300

NO_PLATFORM = "голос по SDK недоступен на этой платформе"

#: Коды HCNetSDK (NET_DVR_GetLastError) при входе — словами для владельца.
_SDK_ERRORS = {
    1: "неверный логин или пароль терминала",
    7: "терминал не отвечает на порту SDK",
    10: "терминал не ответил вовремя",
    # 11/31 — ответ StartVoiceCom, пока терминал в своём режиме вызова
    # (живой DS-K1T341AM, 0.3.6): голос занят его звонком.
    11: "терминал не открыл голос — занят своим вызовом",
    31: "терминал занят",
    153: "учётная запись терминала заблокирована после неверных паролей",
}


#: Коды «терминал занят своим вызовом»: их лечит reject/hangUp + повтор S (talkroute).
BUSY_CODES = (11, 31)


class SDKAudioError(Exception):
    """Голос по SDK не открылся или оборвался; текст — для журнала и атрибута.

    `code` — код HCNetSDK из ответа помощника (None, если ответа не было):
    по нему talkroute решает, повторять ли открытие.
    """

    def __init__(self, text: str, code: int | None = None) -> None:
        super().__init__(text)
        self.code = code


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
