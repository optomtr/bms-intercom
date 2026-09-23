"""Device controller for a single BMS Intercom panel (real or demo)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    ALERT_BACKOFF_MAX,
    ALERT_BACKOFF_START,
    CALL_POLL_INTERVAL,
    CONF_CALL_TIMEOUT,
    CONF_CHANNEL,
    CONF_DOOR_NO,
    CONF_HTTP_PORT,
    CONF_HTTPS_URL,
    CONF_MODE,
    CONF_PROXY_PORT,
    CONF_RTSP_PORT,
    CONF_USE_ALERT_STREAM,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_CHANNEL,
    DEFAULT_DOOR_NO,
    DEFAULT_HTTP_PORT,
    DEFAULT_NAME,
    DEFAULT_PROXY_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_USE_ALERT_STREAM,
    MODE_DEMO,
    SIGNAL_STATE_UPDATED,
)
from .events import call_state_from_event
from .isapi import (
    STATUS_ANSWERED,
    STATUS_RINGING,
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    ISAPIUnsupported,
)
from .probe import format_probe_report, report_attributes

_LOGGER = logging.getLogger(__name__)

# Call states
STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_ANSWERED = "answered"

# Panel call-status string -> internal call state.
_ISAPI_STATUS_TO_STATE = {
    STATUS_RINGING: STATE_RINGING,
    STATUS_ANSWERED: STATE_ANSWERED,
}


class BMSIntercomDevice:
    """Holds the call state and exposes the actions the UI can trigger.

    In demo mode the actions only update local state so the whole flow can be
    tested without hardware. In real mode they talk to the panel over ISAPI:
    the call state comes from the long-lived event stream (alertStream) when
    the panel supports it, otherwise from a poller.

    The panel being unreachable never makes entities disappear — it only makes
    the call state unknown, so the owner can still press «Проверить панель».
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.name: str = entry.data.get(CONF_NAME, DEFAULT_NAME)
        self.mode: str = entry.data.get(CONF_MODE, MODE_DEMO)
        self.call_state: str = STATE_IDLE
        # None = not known yet (nothing tried), True/False = last verdict.
        self.panel_available: bool | None = None
        self.last_error: str = ""
        self.probe_attributes: dict[str, Any] = {}
        self._client: ISAPIClient | None = None
        self._unsub_poll = None
        self._unsub_call_timeout = None
        self._alert_task: asyncio.Task | None = None
        self._use_alert_stream: bool = True

    # --- options -----------------------------------------------------------
    def _opt(self, key: str, default):
        """Options win over the original config-entry data."""
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def is_demo(self) -> bool:
        return self.mode == MODE_DEMO

    @property
    def call_active(self) -> bool:
        return self.call_state in (STATE_RINGING, STATE_ANSWERED)

    @property
    def call_timeout(self) -> int:
        return int(self._opt(CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT))

    @property
    def channel(self) -> int:
        return int(self._opt(CONF_CHANNEL, DEFAULT_CHANNEL))

    @property
    def https_url(self) -> str | None:
        """Optional explicit HTTPS address override (secure context)."""
        return self.entry.options.get(CONF_HTTPS_URL) or None

    @property
    def proxy_port(self) -> int:
        """Port of the built-in auto HTTPS endpoint."""
        return self.entry.options.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT)

    @property
    def client(self) -> ISAPIClient | None:
        return self._client

    @property
    def rtsp_url(self) -> str | None:
        """RTSP main-stream URL of the panel (real mode only)."""
        if self._client is None:
            return None
        return self._client.rtsp_url()

    @property
    def rtsp_url_redacted(self) -> str | None:
        if self._client is None:
            return None
        return self._client.rtsp_url(redacted=True)

    # --- lifecycle ---------------------------------------------------------
    async def async_setup(self) -> None:
        """Prepare the device; in real mode start the event stream or poller."""
        _LOGGER.debug("Настройка домофона '%s' в режиме %s", self.name, self.mode)
        if self.is_demo:
            self.panel_available = True
            return

        self._client = ISAPIClient(
            self.entry.data[CONF_HOST],
            self.entry.data.get(CONF_USERNAME, ""),
            self.entry.data.get(CONF_PASSWORD, ""),
            http_port=self._opt(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            rtsp_port=self._opt(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
            door_no=self._opt(CONF_DOOR_NO, DEFAULT_DOOR_NO),
            channel=self.channel,
        )
        # Digest is proven on this hardware: never start pinned to a scheme
        # remembered from an earlier, broken session.
        self._client.reset_auth()
        self._use_alert_stream = bool(
            self._opt(CONF_USE_ALERT_STREAM, DEFAULT_USE_ALERT_STREAM)
        )
        if self._use_alert_stream:
            self._start_alert_stream()
        else:
            self._start_poll()

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

    async def async_shutdown(self) -> None:
        """Stop the listener/poller and close the ISAPI client."""
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        self._cancel_call_timeout()
        if self._alert_task is not None:
            self._alert_task.cancel()
            try:
                await self._alert_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._alert_task = None
        if self._client is not None:
            await self._client.async_close()
            self._client = None

    @callback
    def _notify(self) -> None:
        """Tell all entities of this device to refresh their state."""
        async_dispatcher_send(
            self.hass, SIGNAL_STATE_UPDATED.format(self.entry.entry_id)
        )

    # --- panel availability ------------------------------------------------
    @callback
    def _set_available(self, available: bool, error: str = "") -> None:
        changed = self.panel_available is not available
        self.panel_available = available
        self.last_error = "" if available else error
        if changed:
            if available:
                _LOGGER.info("[%s] Связь с панелью восстановлена", self.name)
            else:
                _LOGGER.warning("[%s] Панель недоступна: %s", self.name, error)
            if not available:
                # Unreachable panel: the call state is unknown, not "idle".
                self._cancel_call_timeout()
            self._notify()

    # --- event stream ------------------------------------------------------
    async def _async_alert_loop(self) -> None:
        """Keep the alertStream connection up, with reconnect and backoff."""
        backoff = ALERT_BACKOFF_START
        while True:
            try:
                assert self._client is not None
                async for event in self._client.async_iter_alerts(
                    on_connect=lambda: self._set_available(True)
                ):
                    backoff = ALERT_BACKOFF_START
                    state = call_state_from_event(event)
                    _LOGGER.debug(
                        "[%s] Событие панели: type=%s state=%s -> %s",
                        self.name, event.get("type"), event.get("state"), state,
                    )
                    if state is not None:
                        self._apply_call_state(state)
                _LOGGER.debug("[%s] Поток событий закрыт панелью", self.name)
            except asyncio.CancelledError:
                raise
            except ISAPIUnsupported as err:
                _LOGGER.warning(
                    "[%s] %s. Переходим на опрос статуса вызова.", self.name, err
                )
                self._use_alert_stream = False
                self._start_poll()
                return
            except ISAPIAuthError as err:
                self._set_available(False, str(err))
                backoff = ALERT_BACKOFF_MAX
            except ISAPIError as err:
                self._set_available(False, str(err))
                backoff = min(backoff * 2, ALERT_BACKOFF_MAX)
            except Exception as err:  # noqa: BLE001 - listener must never die
                _LOGGER.exception("[%s] Сбой потока событий: %s", self.name, err)
                self._set_available(False, str(err))
                backoff = min(backoff * 2, ALERT_BACKOFF_MAX)
            await asyncio.sleep(backoff)

    # --- polling -----------------------------------------------------------
    async def _async_poll(self, _now) -> None:
        """Poll fallback: read the panel's call status and reflect it locally."""
        if self._client is None:
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
            self._set_available(False, str(err))
            return

        self._set_available(True)
        self._apply_call_state(_ISAPI_STATUS_TO_STATE.get(raw, STATE_IDLE))

    # --- call state --------------------------------------------------------
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
    def _arm_call_timeout(self) -> None:
        """A call that never gets an end event must not hang forever."""
        self._cancel_call_timeout()
        timeout = self.call_timeout
        if timeout <= 0:
            return

        @callback
        def _expire(_now) -> None:
            self._unsub_call_timeout = None
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

    # --- Actions -----------------------------------------------------------
    async def async_simulate_call(self) -> None:
        """Demo only: pretend the panel started ringing."""
        _LOGGER.info("[%s] Симуляция входящего вызова", self.name)
        self._apply_call_state(STATE_RINGING)

    async def async_answer(self) -> None:
        """Answer the call (pick up the handset)."""
        if self.is_demo:
            _LOGGER.info("[%s] Вызов принят (демо)", self.name)
        elif self._client is not None:
            try:
                supported = await self._client.async_answer()
            except ISAPIError as err:
                _LOGGER.error("[%s] Не удалось ответить: %s", self.name, err)
                self._set_available(False, str(err))
                return
            self._set_available(True)
            if not supported:
                _LOGGER.info(
                    "[%s] У модели нет команды «ответить» — только разговор "
                    "через поток и реле двери", self.name
                )
        self._apply_call_state(STATE_ANSWERED)

    async def async_reject(self) -> None:
        """Reject / hang up the call."""
        if self.is_demo:
            _LOGGER.info("[%s] Вызов сброшен (демо)", self.name)
        elif self._client is not None:
            try:
                supported = await self._client.async_reject()
            except ISAPIError as err:
                _LOGGER.error("[%s] Не удалось сбросить: %s", self.name, err)
                self._set_available(False, str(err))
                return
            self._set_available(True)
            if not supported:
                _LOGGER.info("[%s] У модели нет команды «сбросить»", self.name)
        self._apply_call_state(STATE_IDLE)

    async def async_open_door(self) -> None:
        """Open the door relay."""
        if self.is_demo:
            _LOGGER.info("[%s] Дверь открыта (демо)", self.name)
            return
        if self._client is None:
            return
        try:
            await self._client.async_open_door()
        except ISAPIError as err:
            _LOGGER.error("[%s] Не удалось открыть дверь: %s", self.name, err)
            self._set_available(False, str(err))
            return
        self._set_available(True)
        _LOGGER.info("[%s] Команда открытия двери отправлена", self.name)

    # --- diagnostics -------------------------------------------------------
    async def async_probe(self, *, test_door: bool = False) -> str:
        """Probe every candidate endpoint, log the report and keep it in attrs.

        Works regardless of the panel being reachable — that is the point.
        """
        host = self.entry.data.get(CONF_HOST, "")
        if self._client is None:
            report = format_probe_report(
                [], host=host or "демо-режим",
                summary={"режим": "демо — проверять нечего"},
            )
            self.probe_attributes = report_attributes(
                [], host=host, summary={"режим": "демо"}
            )
            _LOGGER.warning("%s", report)
            self._notify()
            return report

        try:
            results, summary = await self._client.async_probe(test_door=test_door)
        except Exception as err:  # noqa: BLE001 - diagnostics must never raise
            _LOGGER.exception("[%s] Проверка панели сорвалась: %s", self.name, err)
            results, summary = [], {"ошибка": str(err)}

        summary.setdefault("порт HTTP", self._opt(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        summary.setdefault("поток событий", "вкл" if self._use_alert_stream else "выкл")
        summary.setdefault(
            "панель на связи",
            {True: "да", False: "нет", None: "не проверялась"}[self.panel_available],
        )
        report = format_probe_report(results, host=host, summary=summary)
        self.probe_attributes = report_attributes(results, host=host, summary=summary)
        # WARNING so it lands in the log without turning on debug for anyone.
        _LOGGER.warning("%s", report)
        if any(r.ok for r in results):
            self._set_available(True)
        self._notify()
        return report

    async def async_snapshot(self) -> bytes | None:
        """Still image from the panel (real mode)."""
        if self._client is None:
            return None
        try:
            image = await self._client.async_snapshot()
        except ISAPIError as err:
            _LOGGER.debug("[%s] Снимок не получен: %s", self.name, err)
            return None
        if image is not None:
            self._set_available(True)
        return image

    async def async_stream_source(self) -> str | None:
        """RTSP URL, with the channel confirmed over ISAPI when possible."""
        if self._client is None:
            return None
        try:
            await self._client.async_select_channel()
        except ISAPIError as err:
            _LOGGER.debug("[%s] Канал не подтверждён: %s", self.name, err)
        return self._client.rtsp_url()
