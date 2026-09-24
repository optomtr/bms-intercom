"""Where the call state comes from: the event stream, or polling.

Split out of device.py to keep both readable (and under the project's
500-line rule). `CallSourceMixin` owns the alertStream listener, the
callStatus poller and the call-state machine; BMSIntercomDevice owns the
config, the actions and the diagnostics.

DS-K1T341AM V3.2.30 has no alertStream at all (404), so on that hardware the
poller IS the doorbell and runs every second.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    ALERT_BACKOFF_MAX,
    ALERT_BACKOFF_START,
    CALL_POLL_ERROR_BACKOFF,
    CALL_POLL_INTERVAL,
    CONF_ALERT_STREAM_SUPPORTED,
)
from .events import call_state_from_event
from .isapi import ISAPIAuthError, ISAPIError, ISAPIUnsupported
from .transport import describe_error

_LOGGER = logging.getLogger(__name__)

#: Give up on the stream for this session after this many failures in a row
#: without it ever connecting (the poller already covers the doorbell).
STREAM_GIVE_UP = 3

#: Entry ids whose next update must NOT trigger a reload — the integration
#: writing down what it learned about the panel is not a user change.
_SKIP_RELOAD = f"{__name__}_skip_reload"

# Call states
STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_ANSWERED = "answered"
#: What ISAPIClient.async_get_call_status() may return besides None (unknown).
_KNOWN_STATES = frozenset({STATE_IDLE, STATE_RINGING, STATE_ANSWERED})

# Две поправки из форка (Abdunazar7, проверены на DS-KV6113):
#: После «Ответить» в HA держим разговор до «Сбросить» или столько секунд,
#: что бы ни рапортовала панель: вилла-панель быстро говорит idle (она звонит
#: мониторам / Hik-Connect, а не HA), а звук и видео идут мимо её статуса.
MAX_TALK_SECONDS = 180
#: Начавшийся звонок держим хотя бы столько секунд, даже если панель бросила
#: вызов через секунду (нет интернета → не достучалась до Hik-Connect):
#: оператор должен успеть увидеть поп-ап и ответить.
RING_WINDOW_SECONDS = 25


class CallSourceMixin:
    """Event stream / polling half of BMSIntercomDevice."""

    def _start_alert_stream(self) -> None:
        if self._alert_task is None or self._alert_task.done():
            self._alert_task = self.entry.async_create_background_task(
                self.hass, self._async_alert_loop(), f"{self.name} alertStream"
            )

    def _start_poll(self) -> None:
        if self._unsub_poll is None:
            self._unsub_poll = async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=CALL_POLL_INTERVAL)
            )

    # --- event stream ------------------------------------------------------
    async def _async_alert_loop(self) -> None:
        """Keep the alertStream connection up, with reconnect and backoff.

        Until the stream has connected at least once, the callStatus poller
        runs alongside it, so a visitor is never missed while we find out
        what this firmware supports. Then:
          * 404/405/… on the stream  -> unsupported, remembered in the entry,
            polling only (never asked again after a restart);
          * STREAM_GIVE_UP failures in a row without ever connecting -> keep
            polling for this session (not persisted: the cause is unclear);
          * the stream connects -> the poller is stopped.
        """
        backoff = ALERT_BACKOFF_START
        connected_once = False
        failures = 0

        def _connected() -> None:
            nonlocal connected_once, failures
            connected_once = True
            failures = 0
            self._set_available(True)
            if self._unsub_poll is not None:
                _LOGGER.info("[%s] Поток событий подключён — опрос остановлен", self.name)
                self._unsub_poll()
                self._unsub_poll = None

        while True:
            try:
                assert self._client is not None
                async for event in self._client.async_iter_alerts(on_connect=_connected):
                    backoff = ALERT_BACKOFF_START
                    state = call_state_from_event(event)
                    _LOGGER.debug(
                        "[%s] Событие панели: type=%s state=%s -> %s",
                        self.name, event.get("type"), event.get("state"), state,
                    )
                    if state is not None:
                        self._apply_panel_state(state)
                _LOGGER.debug("[%s] Поток событий закрыт панелью", self.name)
            except asyncio.CancelledError:
                raise
            except ISAPIUnsupported as err:
                _LOGGER.warning(
                    "[%s] %s. Переходим на опрос статуса вызова.", self.name, err
                )
                self._use_alert_stream = False
                self._remember_no_alert_stream()
                self._start_poll()
                self._notify()
                return
            except ISAPIAuthError as err:
                self._stream_failed(str(err))
                backoff = ALERT_BACKOFF_MAX
            except ISAPIError as err:
                self._stream_failed(str(err))
                backoff = min(backoff * 2, ALERT_BACKOFF_MAX)
            except Exception as err:  # noqa: BLE001 - listener must never die
                _LOGGER.exception("[%s] Сбой потока событий: %s", self.name, err)
                self._stream_failed(describe_error(err))
                backoff = min(backoff * 2, ALERT_BACKOFF_MAX)

            if not connected_once:
                failures += 1
                if failures >= STREAM_GIVE_UP:
                    _LOGGER.warning(
                        "[%s] Поток событий не подключился %s раз подряд — "
                        "до перезапуска работаем опросом callStatus",
                        self.name, failures,
                    )
                    self._use_alert_stream = False
                    self._start_poll()
                    self._notify()
                    return
            await asyncio.sleep(backoff)

    @callback
    def _stream_failed(self, reason: str) -> None:
        """A stream failure. Covered by polling until the stream is proven."""
        if self._unsub_poll is None:
            _LOGGER.info(
                "[%s] Поток событий недоступен (%s) — пока опрашиваем callStatus",
                self.name, reason,
            )
            self._start_poll()
            self._notify()
            return
        # The poller now owns panel availability; a stream hiccup must not
        # flip the entities between "on line" and "off line" every retry.
        _LOGGER.debug("[%s] Поток событий: %s", self.name, reason)

    @callback
    def _remember_no_alert_stream(self) -> None:
        """Persist the 404 verdict so restarts do not re-discover it.

        Written into the entry data, not the options, and the reload listener
        is told to ignore this particular change — otherwise the integration
        would restart itself the moment it learns something about itself.
        """
        if self.entry.data.get(CONF_ALERT_STREAM_SUPPORTED) is False:
            return
        skip: set[str] = self.hass.data.setdefault(_SKIP_RELOAD, set())
        skip.add(self.entry.entry_id)
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_ALERT_STREAM_SUPPORTED: False},
        )
        _LOGGER.info(
            "[%s] Запомнили: у модели нет потока событий, работаем опросом",
            self.name,
        )

    # --- polling -----------------------------------------------------------
    async def _async_poll(self, now) -> None:
        """Poll fallback: read the panel's call status and reflect it locally.

        On this hardware the poll IS the doorbell (no event stream), so it
        runs every second — but a panel that errors gets a breather instead of
        a request per second.
        """
        if self._client is None:
            return
        if now is not None and time.monotonic() < self._poll_blocked_until:
            return
        try:
            raw = await self._client.async_get_call_status()
        except ISAPIUnsupported as err:
            # Nothing to poll on this model: stop hammering it every 1.5 s.
            _LOGGER.warning(
                "[%s] Опрос статуса вызова недоступен (%s). Состояние вызова "
                "будет только по потоку событий.", self.name, err,
            )
            if self._unsub_poll is not None:
                self._unsub_poll()
                self._unsub_poll = None
            return
        except ISAPIError as err:
            self._poll_blocked_until = time.monotonic() + CALL_POLL_ERROR_BACKOFF
            self._set_available(False, str(err))
            return

        self._poll_blocked_until = 0.0
        self._set_available(True)
        if raw is None:
            return  # unknown word from the panel: keep the state we had
        if raw in _KNOWN_STATES:
            self._apply_panel_state(raw)

    # --- call state --------------------------------------------------------
    @callback
    def _apply_panel_state(self, new_state: str) -> None:
        """Статус, который сообщила ПАНЕЛЬ (опрос или поток событий).

        Действия оператора в HA идут мимо — прямо в _apply_call_state.
        """
        now = time.monotonic()
        if self._answered:
            if now - self._answered_at <= MAX_TALK_SECONDS:
                return  # латч разговора: статус панели не закрывает поп-ап
            self._answered = False
        if new_state == STATE_RINGING and self.call_state != STATE_RINGING:
            self._ringing_at = now
        if self.call_state == STATE_RINGING and new_state == STATE_IDLE:
            left = RING_WINDOW_SECONDS - (now - self._ringing_at)
            if left > 0:
                # Окно звонка. Сброс поручаем таймеру вызова: с опросом его и так
                # сделает следующий опрос, а поток событий второго idle не пришлёт.
                self._arm_call_timeout(left)
                return
        self._apply_call_state(new_state)

    @callback
    def _apply_call_state(self, new_state: str) -> None:
        if new_state == self.call_state:
            if new_state != STATE_IDLE:
                self._arm_call_timeout()  # keep the safety net fresh
            return
        _LOGGER.debug(
            "[%s] Статус вызова: %s -> %s", self.name, self.call_state, new_state
        )
        self.call_state = new_state
        if new_state == STATE_IDLE:
            self._cancel_call_timeout()
        else:
            self._arm_call_timeout()
        self._notify()

    @callback
    def _arm_call_timeout(self, seconds: float | None = None) -> None:
        """A call that never gets an end event must not hang forever.

        Пока держится латч разговора, срок — не меньше MAX_TALK_SECONDS:
        иначе обычный таймаут (60 с) оборвал бы разговор, который латч
        как раз должен держать.
        """
        self._cancel_call_timeout()
        timeout = self.call_timeout if seconds is None else seconds
        if timeout <= 0:
            return
        if seconds is None and self._answered:
            timeout = max(timeout, MAX_TALK_SECONDS)

        @callback
        def _expire(_now) -> None:
            self._unsub_call_timeout = None
            self._answered = False
            if self.call_state != STATE_IDLE:
                _LOGGER.info(
                    "[%s] Вызов сброшен по таймауту (%s с)", self.name, timeout
                )
                self.call_state = STATE_IDLE
                self._notify()

        self._unsub_call_timeout = async_call_later(self.hass, timeout, _expire)

    @callback
    def _cancel_call_timeout(self) -> None:
        if self._unsub_call_timeout is not None:
            self._unsub_call_timeout()
            self._unsub_call_timeout = None
