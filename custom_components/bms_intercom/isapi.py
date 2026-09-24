"""Model-agnostic ISAPI client for Hikvision door stations / access terminals.

Verified shapes differ per model (DS-KV door station vs DS-K1T341AM access
terminal vs old DS-KD panels), so every capability has an ordered list of
candidate endpoints (see endpoints.py) and the client keeps the first one that
answers. Auth is HTTP **digest** first (Hikvision's default) with a one-shot
fallback to basic, remembered for the rest of the session.

Nothing here may raise for an endpoint a model simply does not have: a missing
answer/reject endpoint must never make an entity unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Callable

import httpx

from .endpoints import (
    AUTH_DIGEST,
    XML_CT,
    Call,
    DEFAULT_CHANNEL,
    alert_stream_paths,
    call_signal_calls,
    call_status_calls,
    capability_calls,
    door_calls,
    identity_calls,
    redact,
    rtsp_paths,
    rtsp_url,
    select_first_working,
    snapshot_paths,
    status_missing,
    status_ok,
    stream_info_paths,
    tolerate,
)
from .digest import Challenge
from .events import (
    STATE_ANSWERED,
    STATE_IDLE,
    STATE_RINGING,
    AlertStreamParser,
    call_state_from_event,
    parse_document,
)
from .probing import ProbeMixin
from .talkback import between
from .transport import (
    AuthTransportMixin,
    describe_error,
    ISAPIAuthError,
    ISAPIError,
    ISAPIUnsupported,
)

_LOGGER = logging.getLogger(__name__)

# Kept for backwards compatibility with the previous module API.
STATUS_IDLE = STATE_IDLE
STATUS_RINGING = STATE_RINGING
STATUS_ANSWERED = STATE_ANSWERED

# The panel is quiet between calls; reconnect if nothing arrives for this long.
ALERT_READ_TIMEOUT = 300.0
# How long to wait for the stream's response HEADERS. Bounded separately so a
# panel that never answers cannot hold us for the whole read timeout.
ALERT_HANDSHAKE_TIMEOUT = 15.0


class ISAPIClient(AuthTransportMixin, ProbeMixin):
    """Async wrapper around whatever ISAPI dialect the panel speaks."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        http_port: int = 80,
        rtsp_port: int = 554,
        door_no: int = 1,
        channel: int = DEFAULT_CHANNEL,
        timeout: float = 10.0,
        reuse_nonce: bool = False,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._http_port = http_port
        self._rtsp_port = rtsp_port
        self._door_no = door_no
        self._channel = channel or DEFAULT_CHANNEL
        self._base = f"http://{host}:{http_port}"
        self.auth_mode = AUTH_DIGEST
        # Digest state. Hikvision V3.x binds the nonce to the TCP connection,
        # so the challenge and the authenticated retry must share one
        # keep-alive connection — hence a single long-lived client with
        # http1 only, no redirects and no environment proxy in between.
        self._challenge: Challenge | None = None
        self._nc = 0
        # DS-K1T341AM V3.2.30 issues ONE-SHOT nonces: a request built on a
        # nonce that was already used comes back 401 (often without
        # stale=true). So by default every request takes its own challenge —
        # go out unauthenticated, answer the 401 that comes back with nc=1 and
        # a fresh cnonce. `reuse_nonce=True` turns the one-round-trip fast
        # path back on for firmwares that tolerate it; it falls back to a
        # fresh challenge the moment a 401 arrives.
        self._reuse_nonce = reuse_nonce
        # Diagnostics (no secrets): what the panel asked for and what we sent.
        self.first_challenge_raw: str = ""
        self.last_challenge_raw: str = ""
        self.last_auth_header: str = ""
        #: Every auth attempt of the last request: (scheme, header, status).
        #: The basic fallback must never hide what digest sent and got back.
        self.last_attempts: list[tuple[str, str, int]] = []
        #: True once any scheme returned 2xx — stops the one-shot basic rescue.
        self._auth_proven = False
        self._basic_rescue_tried = False
        # verify=False keeps client creation off the event loop's blocking path
        # (no certifi load) — we only ever talk plain HTTP to the panel anyway.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=False,
            http1=True,
            http2=False,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=4, max_keepalive_connections=4, keepalive_expiry=300.0
            ),
        )
        # Endpoint shapes learned at runtime.
        self._rtsp_path: str | None = None
        self._snapshot_path: str | None = None
        self._identity_call: Call | None = None
        self._door_call: Call | None = None
        self._status_call: Call | None = None
        self._signal_calls: dict[str, Call | None] = {}
        self._capability_call: Call | None = None
        #: None = not established yet, False = this firmware does not have it.
        self.snapshot_supported: bool | None = None
        self.call_signal_supported: bool | None = None
        self.alert_stream_supported: bool | None = None
        self.capabilities_raw: str = ""
        # Filled by async_probe(): extra report sections and the event log.
        self.probe_sections: list[str] = []
        self.probe_acs_events: list[dict[str, Any]] = []
        self._unknown_status_logged: set[str] = set()

    # --- capabilities ------------------------------------------------------
    async def async_verify(self) -> str:
        """Reachability/credentials check used by the config flow."""
        await self._call_selected(identity_calls(), "_identity_call")
        assert self._identity_call is not None
        return self._identity_call.path

    async def async_select_channel(self) -> str:
        """Find a video channel that exists and build the RTSP path from it."""
        if self._rtsp_path is not None:
            return self._rtsp_path

        async def attempt(path: str) -> bool:
            resp = await self._send("GET", path)
            return status_ok(resp.status_code)

        found = await select_first_working(stream_info_paths(self._channel), attempt)
        if found is not None:
            channel = found.rsplit("/", 1)[-1]
            self._rtsp_path = f"/Streaming/Channels/{channel}"
        else:
            self._rtsp_path = rtsp_paths(self._channel)[0]
            _LOGGER.debug(
                "Канал не подтверждён по ISAPI, берём путь по умолчанию %s",
                self._rtsp_path,
            )
        return self._rtsp_path

    def rtsp_url(self, *, redacted: bool = False) -> str:
        path = self._rtsp_path or rtsp_paths(self._channel)[0]
        return rtsp_url(
            self._host, self._rtsp_port, self._username, self._password, path,
            redacted=redacted,
        )

    async def async_snapshot(self) -> bytes | None:
        """Still image from the panel, or None if this model has none.

        DS-K1T341AM V3.2.30 answers 404 on every picture endpoint. After the
        first full walk we remember that and stop asking — the camera then
        takes its stills from the RTSP stream instead.
        """
        if self.snapshot_supported is False:
            return None

        async def attempt(path: str) -> bool:
            resp = await self._send("GET", path)
            if not status_ok(resp.status_code) or not resp.content:
                return False
            self._last_snapshot = resp.content
            return True

        candidates = (
            (self._snapshot_path,) if self._snapshot_path
            else snapshot_paths(self._channel)
        )
        self._last_snapshot = None
        chosen = await select_first_working(candidates, attempt)
        if chosen is None:
            self._snapshot_path = None
            if self.snapshot_supported is None:
                _LOGGER.info(
                    "Панель не отдаёт снимок по ISAPI — кадры будем брать из потока"
                )
            self.snapshot_supported = False
            return None
        self._snapshot_path = chosen
        self.snapshot_supported = True
        return self._last_snapshot

    _last_snapshot: bytes | None = None

    async def async_get_call_status(self) -> str | None:
        """Poll the call status.

        Returns idle / ringing / answered, or **None** when the panel sent a
        word we do not know — the caller then keeps the state it had rather
        than pretending the call ended. The real shape on DS-K1T341AM is
        `{"CallStatus": {"status": "idle"}}`.
        """
        resp = await self._call_selected(call_status_calls(), "_status_call")
        event = parse_document(resp.text) or {}
        # The poll answer is a status document, not an alert; force the
        # call-event reading so the shared word map applies.
        event.setdefault("fields", {})
        event["type"] = event.get("type") or "callstatus"
        state = call_state_from_event(event)
        if state is None:
            raw = event.get("fields", {}).get("status", "")
            if raw not in self._unknown_status_logged:
                self._unknown_status_logged.add(raw)
                _LOGGER.warning(
                    "Неизвестный статус вызова от панели: %r (оставляем прежнее "
                    "состояние). Сообщите это значение разработчику.", raw,
                )
        return state

    async def async_load_capabilities(self) -> bool:
        """Read VideoIntercom/capabilities and see if answer/reject exist.

        DS-K1T341AM publishes the document but has no callSignal endpoint, so
        the buttons must degrade to local-only instead of erroring.
        """
        if self.call_signal_supported is not None:
            return self.call_signal_supported
        try:
            resp = await self._call_selected(capability_calls(), "_capability_call")
        except ISAPIError as err:
            _LOGGER.debug("Возможности домофона не прочитаны: %s", err)
            return False
        self.capabilities_raw = resp.text
        body = resp.text.lower()
        self.call_signal_supported = "callsignal" in body or "cmdtype" in body
        _LOGGER.info(
            "Панель %s команды ответа/сброса (по capabilities)",
            "поддерживает" if self.call_signal_supported else "не поддерживает",
        )
        return self.call_signal_supported

    async def async_answer(self) -> bool:
        return await self.async_signal("answer")

    async def async_reject(self) -> bool:
        return await self.async_signal("reject")

    async def async_hangup(self) -> bool:
        """Положить трубку в УЖЕ отвеченном вызове (из форка, DS-KV6113).

        Hikvision различает: `reject` отклоняет только звонящий вызов, а идущий
        разговор завершает `hangUp`.
        """
        return await self.async_signal("hangUp")

    async def async_signal(self, cmd: str) -> bool:
        """Send `answer`/`reject`; False when the model has no such command."""
        return await self._async_signal(cmd)

    async def _async_signal(self, cmd: str) -> bool:
        """Answer/reject where supported. Returns False when the model has none."""
        if self.call_signal_supported is False:
            return False
        if cmd in self._signal_calls and self._signal_calls[cmd] is None:
            return False
        cached = self._signal_calls.get(cmd)
        candidates = (cached,) if cached else call_signal_calls(cmd)
        last_status: int | None = None

        async def attempt(call: Call) -> bool:
            nonlocal last_status
            resp = await self._send(
                call.method, call.path, body=call.body, headers=call.headers or None
            )
            last_status = resp.status_code
            return status_ok(resp.status_code)

        chosen = await select_first_working(candidates, attempt)
        self._signal_calls[cmd] = chosen
        if chosen is not None:
            self.call_signal_supported = True
            return True
        if tolerate(last_status):
            # Модель, у которой callSignal уже подтверждён, отказала именно
            # в этой команде (например, hangUp/reject не к месту по состоянию
            # вызова) — это не повод выключать ответ/сброс на всю сессию и не
            # повод запоминать команду как несуществующую.
            if self.call_signal_supported is True:
                self._signal_calls.pop(cmd, None)
            else:
                self.call_signal_supported = False
            _LOGGER.info(
                "Панель не приняла «%s» (HTTP %s) — команда пропущена",
                cmd, last_status,
            )
            return False
        raise ISAPIError(f"{cmd}: HTTP {last_status}")

    async def async_open_door(self) -> None:
        """Open the door relay using whichever shape this model accepts."""
        await self._call_selected(door_calls(self._door_no), "_door_call")

    async def async_ensure_twoway_codec(self, codec: str = "G.711ulaw") -> None:
        """Best-effort: кодек two-way audio панели = G.711 µ-law (из форка).

        Браузер шлёт G.711 µ-law (talkback.py); совпадение с панелью избавляет
        от перекодирования. Уже стоит — ничего не делаем. Модель без
        two-way audio (DS-K1T341AM) отвечает 404 — ISAPIError ловит вызывающий.
        """
        base = "/ISAPI/System/TwoWayAudio/channels"
        resp = await self._request(Call("twoWayAudio.channels", "GET", base))
        cid = between(resp.text, "<id>", "<") or "1"
        if between(resp.text, "<audioCompressionType>", "<") == codec:
            return
        resp = await self._request(Call("twoWayAudio.channel", "GET", f"{base}/{cid}"))
        if "<audioCompressionType>" not in resp.text:
            return
        new_xml = re.sub(
            r"<audioCompressionType>.*?</audioCompressionType>",
            f"<audioCompressionType>{codec}</audioCompressionType>",
            resp.text, count=1,
        )
        await self._request(
            Call("twoWayAudio.codec", "PUT", f"{base}/{cid}", new_xml, XML_CT)
        )
        _LOGGER.info("Кодек two-way audio установлен в %s (канал %s)", codec, cid)

    # --- event stream ------------------------------------------------------
    async def async_iter_alerts(
        self, on_connect: Callable[[], None] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed events from the long-lived alertStream connection.

        The handshake (response headers) is bounded by ALERT_HANDSHAKE_TIMEOUT;
        only an established 200 stream gets the long read timeout. A 401 body
        is never read — the challenge is in the headers, and a panel that does
        not terminate that body would otherwise hang us for the whole read
        timeout and surface as an empty-message timeout instead of the 404.
        """
        path = alert_stream_paths()[0]
        url = f"{self._base}{path}"
        parser = AlertStreamParser()
        timeout = httpx.Timeout(10.0, read=ALERT_READ_TIMEOUT)
        # First attempt with what we know, second answering the 401 challenge.
        for attempt in (0, 1):
            headers: dict[str, str] = {}
            sent = self._stream_authorization("GET", path, attempt)
            if sent:
                headers["Authorization"] = sent
            request = self._client.build_request(
                "GET", url, headers=headers, timeout=timeout
            )
            try:
                resp = await asyncio.wait_for(
                    self._client.send(request, stream=True),
                    ALERT_HANDSHAKE_TIMEOUT,
                )
            except (httpx.HTTPError, TimeoutError) as err:
                raise ISAPIError(
                    f"alertStream: {redact(describe_error(err), *self.secrets)}"
                ) from err
            try:
                if resp.status_code == 401:
                    if not self._learn_challenge(resp):
                        raise ISAPIAuthError("alertStream: 401 Unauthorized")
                    continue  # retry with the fresh challenge (finally closes)
                if status_missing(resp.status_code):
                    self.alert_stream_supported = False
                    raise ISAPIUnsupported(
                        f"alertStream: HTTP {resp.status_code} — "
                        "модель не умеет поток событий"
                    )
                if not status_ok(resp.status_code):
                    raise ISAPIError(f"alertStream: HTTP {resp.status_code}")
                _LOGGER.debug("alertStream подключён (auth=%s)", self.auth_mode)
                self.alert_stream_supported = True
                if on_connect is not None:
                    on_connect()
                try:
                    async for chunk in resp.aiter_bytes():
                        for event in parser.feed(chunk):
                            yield event
                except httpx.HTTPError as err:
                    raise ISAPIError(
                        f"alertStream: {redact(describe_error(err), *self.secrets)}"
                    ) from err
                return
            finally:
                await resp.aclose()
        raise ISAPIAuthError("alertStream: 401 Unauthorized")
