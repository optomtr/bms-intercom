"""Device controller for a single BMS Intercom panel (real or demo)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ALERT_STREAM_SUPPORTED,
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
from .callsource import (
    STATE_ANSWERED,
    STATE_IDLE,
    STATE_RINGING,
    CallSourceMixin,
)
from .events import PanelEventLog
from .isapi import ISAPIClient, ISAPIError
from .probe import format_probe_report, report_attributes
from .talkback import TwoWayAudioError, TwoWayAudioSession

_LOGGER = logging.getLogger(__name__)


class BMSIntercomDevice(CallSourceMixin):
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
        self._poll_blocked_until: float = 0.0
        # Из форка: idle-просмотр (переключатель «Просмотр»), микрофон к панели,
        # латч разговора и окно звонка (см. callsource._apply_panel_state).
        self.view_active: bool = False
        self._talk: TwoWayAudioSession | None = None
        self._talk_bytes = 0
        self._talk_logged_at = 0
        self._answered = False
        self._answered_at = 0.0
        self._ringing_at = 0.0
        # Диагностика: последние нераспознанные события (атрибут panel_events).
        self.panel_event_log = PanelEventLog()

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
    def call_source(self) -> str:
        """Where the call state comes from on this panel."""
        if self.is_demo:
            return "демо"
        return "поток событий" if self._use_alert_stream else "опрос callStatus"

    @property
    def signal_supported(self) -> bool | None:
        """Does the panel have answer/reject at all? None = not established."""
        if self.is_demo:
            return True
        return None if self._client is None else self._client.call_signal_supported

    @property
    def snapshot_supported(self) -> bool | None:
        if self.is_demo:
            return True
        return None if self._client is None else self._client.snapshot_supported

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
        wanted = bool(self._opt(CONF_USE_ALERT_STREAM, DEFAULT_USE_ALERT_STREAM))
        # Learned earlier that this firmware has no event stream (404)?
        # Then never spend a restart re-discovering it.
        known = self.entry.data.get(CONF_ALERT_STREAM_SUPPORTED)
        self._use_alert_stream = wanted and known is not False
        if wanted and known is False:
            _LOGGER.debug(
                "[%s] Поток событий отключён: модель его не поддерживает", self.name
            )
        if self._use_alert_stream:
            self._start_alert_stream()
        else:
            self._start_poll()
        # Learn the capability profile in the background: which commands the
        # panel has, and whether an ISAPI still image exists at all.
        self.entry.async_create_background_task(
            self.hass, self._async_learn_profile(), f"{self.name} profile"
        )

    async def _async_learn_profile(self) -> None:
        """One-off capability discovery, off the setup path."""
        if self._client is None:
            return
        try:
            await self._client.async_load_capabilities()
            await self._client.async_select_channel()
            await self._client.async_snapshot()
        except ISAPIError as err:
            _LOGGER.debug("[%s] Профиль панели не прочитан: %s", self.name, err)
            return
        self._set_available(True)
        self._notify()
        # Авто-настройка из форка: кодек two-way audio = G.711, чтобы микрофон
        # браузера доходил без перекодирования. Здесь, а не в async_setup:
        # недоступная панель не должна задерживать запуск интеграции.
        try:
            await self._client.async_ensure_twoway_codec()
        except ISAPIError as err:
            _LOGGER.debug("[%s] Кодек two-way audio не задан: %s", self.name, err)

    async def async_shutdown(self) -> None:
        """Stop the listener/poller and close the ISAPI client."""
        await self.async_talk_stop()
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

    # --- Микрофон оператора → панель (native ISAPI two-way audio) ----------
    async def async_talk_start(self) -> None:
        """Открыть канал two-way audio панели под микрофон оператора."""
        if self.is_demo:
            return
        host = self.entry.data.get(CONF_HOST)
        if not host:
            return
        # Закрываем прошлую сессию (вкладку закрыли без talk_stop) — иначе к
        # панели останется висеть two-way-сокет и новый не откроется.
        await self.async_talk_stop()
        sess = TwoWayAudioSession(
            host,
            self._opt(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            self.entry.data.get(CONF_USERNAME, ""),
            self.entry.data.get(CONF_PASSWORD, ""),
        )
        try:
            await sess.async_open()
        except Exception as err:  # noqa: BLE001 - микрофон не должен ронять HA
            _LOGGER.warning("[%s] Не удалось открыть микрофон к панели: %s", self.name, err)
            await sess.async_close()
            return
        self._talk = sess
        self._talk_bytes = 0
        self._talk_logged_at = 0
        _LOGGER.debug("[%s] Микрофон к панели открыт (кодек %s)", self.name, sess.codec)

    async def async_talk_send(self, data: bytes) -> None:
        """Передать кусок G.711 от браузера на панель."""
        if self._talk is None:
            return
        try:
            await self._talk.async_send(data)
        except TwoWayAudioError as err:
            _LOGGER.warning("[%s] Микрофон: поток к панели оборвался (%s)", self.name, err)
            await self.async_talk_stop()
            return
        # Раз в ~2 с (16000 байт при 8 кГц) отметить в debug, что звук уходит.
        self._talk_bytes += len(data)
        if self._talk_bytes - self._talk_logged_at >= 16000:
            self._talk_logged_at = self._talk_bytes
            _LOGGER.debug("[%s] Микрофон → панель: отправлено %d Б", self.name, self._talk_bytes)

    async def async_talk_stop(self) -> None:
        """Закрыть канал two-way audio панели."""
        sess, self._talk = self._talk, None
        if sess is not None:
            await sess.async_close()

    # --- Actions -----------------------------------------------------------
    async def async_set_view(self, on: bool) -> None:
        """Открыть/закрыть idle-просмотр (поп-ап без вызова)."""
        if self.view_active != on:
            self.view_active = on
            self._notify()

    async def async_simulate_call(self) -> None:
        """Demo only: pretend the panel started ringing."""
        _LOGGER.info("[%s] Симуляция входящего вызова", self.name)
        self._apply_call_state(STATE_RINGING)

    async def async_answer(self) -> None:
        """Answer the call (pick up the handset)."""
        await self._async_call_signal(("answer",), STATE_ANSWERED)

    async def async_reject(self) -> None:
        """Отклонить звонящий вызов или положить трубку в разговоре.

        Из форка: панель завершает отвеченный вызов командой `hangUp`, а ещё
        звонящий — `reject`. Шлём подходящую по состоянию, при отказе — другую.
        """
        answered = self.call_state == STATE_ANSWERED
        cmds = ("hangUp", "reject") if answered else ("reject", "hangUp")
        await self._async_call_signal(cmds, STATE_IDLE)

    async def _async_call_signal(self, cmds: tuple[str, ...], new_state: str) -> None:
        """Send answer/reject where the model has it; otherwise stay local.

        Commands are tried in order until one is accepted. DS-K1T341AM has no
        callSignal endpoint: the buttons then only move the local call state
        (the popup and the door relay still work), and the entity attributes
        say `answer_supported: no` so nobody is misled.
        """
        word = "принят" if new_state == STATE_ANSWERED else "сброшен"
        if self.is_demo:
            _LOGGER.info("[%s] Вызов %s (демо)", self.name, word)
        elif self._client is not None:
            errors: list[ISAPIError] = []
            for cmd in cmds:
                try:
                    if await self._client.async_signal(cmd):
                        break
                except ISAPIError as err:
                    errors.append(err)
                    _LOGGER.warning("[%s] Команда «%s» не прошла: %s", self.name, cmd, err)
            else:
                if len(errors) == len(cmds):
                    _LOGGER.error("[%s] Вызов не %s: панель отвергла все команды", self.name, word)
                    self._set_available(False, str(errors[-1]))
                    return
                _LOGGER.debug(
                    "[%s] У модели нет команд %s — меняем только состояние "
                    "в Home Assistant", self.name, "/".join(cmds),
                )
            self._set_available(True)
        # Латч разговора (форк): дальше статус панели не закроет поп-ап, пока
        # оператор не нажмёт «Сбросить» (или не выйдет MAX_TALK_SECONDS).
        # В демо панели нет — демо ведёт себя как раньше.
        self._answered = new_state == STATE_ANSWERED and not self.is_demo
        self._answered_at = time.monotonic()
        self._apply_call_state(new_state)

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
    async def async_probe(
        self, *, test_door: bool = False, acs_minutes: int = 15
    ) -> str:
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
            results, summary = await self._client.async_probe(
                test_door=test_door, acs_minutes=acs_minutes
            )
        except Exception as err:  # noqa: BLE001 - diagnostics must never raise
            _LOGGER.exception("[%s] Проверка панели сорвалась: %s", self.name, err)
            results, summary = [], {"ошибка": str(err)}

        summary.setdefault("порт HTTP", self._opt(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        summary.setdefault("поток событий", "вкл" if self._use_alert_stream else "выкл")
        summary.setdefault(
            "панель на связи",
            {True: "да", False: "нет", None: "не проверялась"}[self.panel_available],
        )
        sections = self._client.probe_sections if self._client else []
        events = self._client.probe_acs_events if self._client else []
        report = format_probe_report(
            results, host=host, summary=summary, sections=sections
        )
        self.probe_attributes = report_attributes(
            results, host=host, summary=summary, sections=sections, events=events
        )
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
