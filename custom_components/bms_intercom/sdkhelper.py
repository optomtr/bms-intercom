"""Постоянный помощник `bms_talk --serve`: вход на терминал один раз, голос — за доли секунды.

Зачем (0.3.8): на HA Green NET_DVR_Init перебирает все сетевые адреса хоста
(~3,3 с), вход — ещё ~2,2 с. Однократный помощник 0.3.5–0.3.7 платил это на
каждый «Ответить», а на коде 11 (терминал в своём вызове) — ещё раз новым
процессом: голос включался через ~12 с. Теперь процесс живёт, пока загружена
интеграция, а talk_start шлёт ему только команду «открыть голос».

Когда поднимаем: сразу, как выяснилось talk_via=sdk (профиль прочитан,
selftest прошёл), а не по ringing. Кнопка вызова DS-K1T341AM даёт ring на
1–2 с, оператор жмёт «Ответить» через 1–3 с, а вход занимает ~5,5 с —
поднятый по ringing помощник на первом звонке к «Ответить» не успевал бы.
По ringing — только «пинок» (kick): упавшего помощника поднять сразу, не
дожидаясь паузы перед перезапуском.

Протокол — в sdk/src/serve.h: первая строка stdin — JSON входа (пароль только
так), ответ ready|error; дальше кадры «тип (1 байт) + длина (4 байта LE) +
данные»: S — открыть голос, A — звук, E — закрыть, P — пинг, Q — выход.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections.abc import Callable, Coroutine
from typing import Any

from . import sdkaudio
from .sdkaudio import SDKAudioError, describe_error

_LOGGER = logging.getLogger(__name__)

#: Init (~3,3 с на HA Green) + вход (~2,2 с) + таймаут соединения SDK (3 с) и запас.
READY_TIMEOUT = 20
#: S: StartVoiceCom, а при потерянном входе — ещё и перевход (до ~5 с).
VOICE_START_TIMEOUT = 12
#: E / P / запись в трубу — мгновенные; не ответил за столько — помощник завис.
COMMAND_TIMEOUT = 5
#: Сколько ждём выхода по Q/EOF до terminate/kill.
STOP_TIMEOUT = 3
#: Паузы перед перезапуском упавшего помощника: 1, 2, 4 … 60 с.
RESTART_MIN = 1.0
RESTART_MAX = 60.0
#: Проработал столько после ready — падение разовое, пауза снова с RESTART_MIN.
STABLE_AFTER = 60.0
#: Пинг простаивающего помощника: зависший SDK выяснится до звонка, а не в нём.
PING_EVERY = 120.0
#: Блокировка (153) без срока в ответе — через столько одна новая попытка.
LOCKED_RETRY = 600.0
#: Неверный пароль: новый вход только приблизил бы блокировку учётной записи
#: терминала (153) — помощник больше не поднимаем до перезагрузки интеграции.
FATAL_CODES = (1,)
LOCKED_CODE = 153

_GONE: dict = {"type": "gone"}  # помощник завершился — будит ждущую команду


def _frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("<I", len(payload)) + payload


class SDKHelper:
    """Один постоянный помощник на терминал: вход, перезапуск, команды голоса."""

    def __init__(
        self, name: str, host: str, port: int, user: str, password: str, *,
        create_task: Callable[[Coroutine[Any, Any, None], str], asyncio.Task],
        on_fatal: Callable[[str, int | None], None] | None = None,
    ) -> None:
        self.name = name
        self._login = {"host": host, "port": port, "user": user, "password": password}
        self._secret = password
        self._create_task = create_task
        self._on_fatal = on_fatal
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._replies: asyncio.Queue | None = None
        self._ready = asyncio.Event()
        self._kick = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._said = ""
        self.voice_open = False
        self.codec = "G.711ulaw"
        #: Почему помощник больше не поднимается (неверный пароль); "" — поднимается.
        self.fatal = ""
        #: Почему последний запуск не дошёл до ready — для talk_error.
        self.last_error = ""
        self.runs = 0

    @property
    def ready(self) -> bool:
        """Вход выполнен, помощник ждёт команд."""
        return self._ready.is_set()

    # --- жизнь процесса --------------------------------------------------
    def start(self) -> None:
        """Поднять помощника в фоне (идемпотентно). После неверного пароля — нет."""
        if self.fatal or self._closing:
            return
        if self._task is None or self._task.done():
            self._task = self._create_task(self._async_supervise(), f"{self.name} bms_talk")

    def kick(self) -> None:
        """Звонок или «Ответить»: упавшего помощника поднять сейчас, без паузы."""
        self.start()
        self._kick.set()

    async def async_wait_ready(self, timeout: float) -> bool:
        """Дождаться входа не дольше timeout; неверный пароль — сразу False."""
        deadline = time.monotonic() + timeout
        while not self._ready.is_set() and not self.fatal:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self._ready.wait(), min(left, 0.1))
            except TimeoutError:
                continue
        return self._ready.is_set()

    async def async_close(self) -> None:
        """Выгрузка интеграции: Q, дождаться выхода (Logout на терминале), снять задачу."""
        self._closing = True
        await self._async_stop_proc()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _fail(self, text: str, code: int | None = None) -> None:
        self.fatal = self.last_error = text
        if self._on_fatal is not None:
            self._on_fatal(text, code)

    def _say(self, why: str, then: str) -> None:
        # Терминал выключен — перезапуск каждую минуту; повтор той же причины — в DEBUG.
        level = logging.DEBUG if why == self._said else logging.WARNING
        self._said = why
        _LOGGER.log(level, "[%s] Помощник голоса: %s — %s", self.name, why, then)

    async def _async_supervise(self) -> None:
        pause = RESTART_MIN
        try:
            while not self._closing and not self.fatal:
                try:
                    ready_at, code, why, lock_s = await self._async_run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - надзор не должен умирать
                    _LOGGER.exception("[%s] Сбой помощника голоса: %s", self.name, err)
                    ready_at, code, why, lock_s = 0.0, None, f"сбой помощника голоса: {err!r}", 0
                if self._closing or self.fatal:
                    return
                if code in FATAL_CODES or why == sdkaudio.NO_PLATFORM:
                    self._fail(why, code)
                    return
                self.last_error = why
                if code == LOCKED_CODE:
                    # Учётка заблокирована: пинок не ускоряет — вход сейчас продлил бы блокировку.
                    delay = float(lock_s) + 5 if lock_s > 0 else LOCKED_RETRY
                    self._say(why, f"новая попытка входа через {delay:g} с")
                    await asyncio.sleep(delay)
                    continue
                if ready_at and time.monotonic() - ready_at >= STABLE_AFTER:
                    pause = RESTART_MIN
                self._say(why, f"перезапуск через {pause:g} с")
                try:
                    await asyncio.wait_for(self._kick.wait(), pause)
                except TimeoutError:
                    pass
                pause = min(pause * 2, RESTART_MAX)
        finally:
            await self._async_stop_proc()

    async def _async_run_once(self) -> tuple[float, int | None, str, int]:
        """Один процесс от запуска до выхода → (когда ready или 0, код, почему, lock_s)."""
        self._kick.clear()  # пинок во время этой попытки ускорит следующую
        helper = await sdkaudio._async_helper()
        if isinstance(helper, str):
            return 0.0, None, helper, 0
        cmd, cwd = helper
        self.runs += 1
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, "--serve", cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            return 0.0, None, f"помощник голоса не запустился: {err}", 0
        self._proc, self._replies = proc, asyncio.Queue()
        pump = asyncio.create_task(self._async_pump_stderr(proc))
        try:
            try:
                proc.stdin.write(json.dumps(self._login).encode() + b"\n")
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), READY_TIMEOUT)
            except TimeoutError:
                return 0.0, None, f"помощник голоса не вошёл на терминал за {READY_TIMEOUT} с", 0
            except (BrokenPipeError, ConnectionResetError):
                line = b""
            reply = sdkaudio._parse_line(line)
            if reply.get("type") != "ready":
                await self._async_stop_proc()
                if reply.get("type") == "error":
                    code = reply.get("code")
                    code = code if isinstance(code, int) else None
                    lock_s = reply.get("lock_seconds")
                    return 0.0, code, describe_error(code, reply.get("message")), \
                        lock_s if isinstance(lock_s, int) else 0
                return 0.0, None, f"помощник голоса завершился без ответа (код выхода {proc.returncode})", 0
            ready_at = time.monotonic()
            self.last_error = self._said = ""
            self._ready.set()
            _LOGGER.info("[%s] Помощник голоса вошёл на терминал (HCNetSDK %s) — «Ответить» "
                         "откроет голос без входа", self.name, reply.get("sdk") or "?")
            await self._async_serve(proc)
            rc = await proc.wait()
            return ready_at, None, f"помощник голоса завершился (код выхода {rc})", 0
        finally:
            self._ready.clear()
            self.voice_open = False
            if self._replies is not None:
                self._replies.put_nowait(_GONE)
            await self._async_stop_proc()
            await asyncio.wait({pump}, timeout=1)  # дочитать последние строки журнала
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    async def _async_serve(self, proc: asyncio.subprocess.Process) -> None:
        """Читать ответы, пока помощник жив; простаивающего — пинговать."""
        reader = asyncio.create_task(self._async_read(proc))
        try:
            while True:
                done, _ = await asyncio.wait({reader}, timeout=PING_EVERY)
                if done:
                    return
                if not self.voice_open:
                    try:
                        await self.async_ping()
                    except SDKAudioError as err:  # завис — _async_command его уже снял
                        _LOGGER.debug("[%s] Пинг помощника: %s", self.name, err)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _async_read(self, proc: asyncio.subprocess.Process) -> None:
        async for raw in proc.stdout:
            reply = sdkaudio._parse_line(raw)
            kind = reply.get("type")
            if kind == "voice_lost":
                self.voice_open = False
                _LOGGER.warning("[%s] Голос к терминалу оборвался: %s", self.name,
                                describe_error(reply.get("code"), reply.get("message")))
            elif kind in ("started", "stopped", "pong", "error") and self._replies is not None:
                self._replies.put_nowait(reply)
            else:
                _LOGGER.debug("[bms_talk] %s", self._clean(raw.decode(errors="replace").rstrip()))

    async def _async_stop_proc(self) -> None:
        """Q и закрыть stdin → помощник сам делает StopVoiceCom/Logout/Cleanup; завис — снять."""
        proc, self._proc = self._proc, None
        self._ready.clear()
        self.voice_open = False
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write(_frame(b"Q"))
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except TimeoutError:
            await sdkaudio._async_kill(proc)

    def _clean(self, text: str) -> str:
        # Помощник чужой: напишет пароль в свой журнал — в журнал HA он всё равно не попадёт.
        return text.replace(self._secret, "***") if self._secret else text

    async def _async_pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        async for raw in proc.stderr:
            _LOGGER.debug("[bms_talk] %s", self._clean(raw.decode(errors="replace").rstrip()))

    # --- команды ---------------------------------------------------------
    async def _async_write(self, proc: asyncio.subprocess.Process, data: bytes) -> None:
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await asyncio.wait_for(proc.stdin.drain(), COMMAND_TIMEOUT)
            except (BrokenPipeError, ConnectionResetError, TimeoutError) as err:
                raise SDKAudioError(f"помощник голоса не принимает команды: {err!r}") from err

    async def _async_command(self, kind: bytes, payload: bytes, timeout: float,
                             expect: tuple[str, ...]) -> dict:
        """Команда → её ответ. Чужой (запоздалый от отменённой команды) пропускаем."""
        async with self._cmd_lock:
            proc, replies = self._proc, self._replies
            if proc is None or replies is None or proc.returncode is not None or not self.ready:
                raise SDKAudioError(self.fatal or "голос по SDK: помощник ещё не вошёл на терминал")
            while not replies.empty():
                replies.get_nowait()
            await self._async_write(proc, _frame(kind, payload))
            deadline = time.monotonic() + timeout
            while True:
                try:
                    reply = await asyncio.wait_for(replies.get(), max(deadline - time.monotonic(), 0))
                except TimeoutError:
                    _LOGGER.warning("[%s] Помощник голоса не ответил на «%s» за %g с — перезапускаю",
                                    self.name, kind.decode(), timeout)
                    await sdkaudio._async_kill(proc)
                    raise SDKAudioError(f"голос по SDK: помощник не ответил за {timeout:g} с") from None
                if reply is _GONE:
                    raise SDKAudioError("голос по SDK: помощник завершился")
                if reply.get("type") in expect:
                    return reply

    async def async_voice_start(self, channel: int = 1) -> str:
        """S → кодек; ошибка терминала — SDKAudioError с кодом (11/31 — занят своим вызовом)."""
        reply = await self._async_command(
            b"S", json.dumps({"channel": channel}).encode(), VOICE_START_TIMEOUT, ("started", "error"))
        if reply.get("type") == "started":
            self.codec = str(reply.get("codec") or self.codec)
            self.voice_open = True
            return self.codec
        code = reply.get("code")
        code = code if isinstance(code, int) else None
        text = describe_error(code, reply.get("message"))
        if code in FATAL_CODES:  # перевход внутри помощника упёрся в пароль
            self._fail(text, code)
        raise SDKAudioError(text, code)

    async def async_send(self, data: bytes) -> None:
        """Кусок G.711 µ-law в открытый голос; голос закрыт/помощник упал — SDKAudioError."""
        proc = self._proc
        if proc is None or proc.returncode is not None or not self.voice_open:
            raise SDKAudioError("голос по SDK закрыт (оборвался или помощник перезапускается)")
        await self._async_write(proc, _frame(b"A", data))

    async def async_voice_stop(self) -> None:
        """E: закрыть голос (безвредно, если он и так закрыт); помощник остаётся со входом."""
        self.voice_open = False
        if not self.ready:
            return
        try:
            await self._async_command(b"E", b"", COMMAND_TIMEOUT, ("stopped",))
        except SDKAudioError as err:
            _LOGGER.debug("[%s] Закрытие голоса: %s", self.name, err)

    async def async_ping(self) -> dict:
        """P → {"logged_in", "voice"}: помощник жив и знает, есть ли вход."""
        reply = await self._async_command(b"P", b"", COMMAND_TIMEOUT, ("pong",))
        _LOGGER.debug("[%s] Помощник голоса: вход %s", self.name,
                      "есть" if reply.get("logged_in") else "потерян (SDK входит сам)")
        return reply


class SDKVoice:
    """Открытый голос на постоянном помощнике — интерфейс как у ISAPI-сессии."""

    def __init__(self, helper: SDKHelper) -> None:
        self._helper = helper
        self.codec = helper.codec

    @property
    def alive(self) -> bool:
        """Голос открыт: второй talk_start едет на нём."""
        return self._helper.voice_open

    async def async_send(self, data: bytes) -> None:
        await self._helper.async_send(data)

    async def async_close(self) -> None:
        await self._helper.async_voice_stop()
