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

import logging
from typing import Any, AsyncIterator, Callable

import httpx

from .endpoints import (
    AUTH_DIGEST,
    Call,
    DEFAULT_CHANNEL,
    alert_stream_paths,
    call_signal_calls,
    call_status_calls,
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
from .transport import (
    AuthTransportMixin,
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
        """Still image from the panel, or None if no shape works."""
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
            return None
        self._snapshot_path = chosen
        return self._last_snapshot

    _last_snapshot: bytes | None = None

    async def async_get_call_status(self) -> str:
        """Poll fallback: one of idle / ringing / answered."""
        resp = await self._call_selected(call_status_calls(), "_status_call")
        event = parse_document(resp.text) or {}
        # The poll answer is a status document, not an alert; force the
        # call-event reading so the shared word map applies.
        event.setdefault("fields", {})
        event["type"] = event.get("type") or "callstatus"
        return call_state_from_event(event) or STATE_IDLE

    async def async_answer(self) -> bool:
        return await self._async_signal("answer")

    async def async_reject(self) -> bool:
        return await self._async_signal("reject")

    async def _async_signal(self, cmd: str) -> bool:
        """Answer/reject where supported. Returns False when the model has none."""
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
            return True
        if tolerate(last_status):
            _LOGGER.info(
                "Панель не поддерживает «%s» (HTTP %s) — команда пропущена",
                cmd, last_status,
            )
            return False
        raise ISAPIError(f"{cmd}: HTTP {last_status}")

    async def async_open_door(self) -> None:
        """Open the door relay using whichever shape this model accepts."""
        await self._call_selected(door_calls(self._door_no), "_door_call")

    # --- event stream ------------------------------------------------------
    async def async_iter_alerts(
        self, on_connect: Callable[[], None] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed events from the long-lived alertStream connection."""
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
            try:
                async with self._client.stream(
                    "GET", url, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status_code == 401:
                        await resp.aread()
                        if not self._learn_challenge(resp):
                            raise ISAPIAuthError("alertStream: 401 Unauthorized")
                        continue  # retry with the fresh challenge
                    if status_missing(resp.status_code):
                        raise ISAPIUnsupported(
                            f"alertStream: HTTP {resp.status_code} — модель не умеет поток событий"
                        )
                    if not status_ok(resp.status_code):
                        raise ISAPIError(f"alertStream: HTTP {resp.status_code}")
                    _LOGGER.debug(
                        "alertStream подключён (auth=%s)", self.auth_mode
                    )
                    if on_connect is not None:
                        on_connect()
                    async for chunk in resp.aiter_bytes():
                        for event in parser.feed(chunk):
                            yield event
                    return
            except httpx.HTTPError as err:
                raise ISAPIError(
                    f"alertStream: {redact(str(err), *self.secrets)}"
                ) from err
        raise ISAPIAuthError("alertStream: 401 Unauthorized")
