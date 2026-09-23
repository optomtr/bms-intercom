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
    AUTH_BASIC,
    AUTH_DIGEST,
    Call,
    DEFAULT_CHANNEL,
    alert_stream_paths,
    call_signal_calls,
    call_status_calls,
    door_calls,
    identity_calls,
    next_auth_mode,
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
from .digest import (
    Challenge,
    basic_authorization,
    build_authorization,
    pick_digest_challenge,
    safe_auth_header,
)
from .events import (
    STATE_ANSWERED,
    STATE_IDLE,
    STATE_RINGING,
    AlertStreamParser,
    call_state_from_event,
    parse_document,
)
from .probing import ProbeMixin

_LOGGER = logging.getLogger(__name__)

# Kept for backwards compatibility with the previous module API.
STATUS_IDLE = STATE_IDLE
STATUS_RINGING = STATE_RINGING
STATUS_ANSWERED = STATE_ANSWERED

# The panel is quiet between calls; reconnect if nothing arrives for this long.
ALERT_READ_TIMEOUT = 300.0


class ISAPIError(Exception):
    """Raised when an ISAPI request fails."""


class ISAPIAuthError(ISAPIError):
    """Credentials rejected by the panel (401 on every auth scheme)."""


class ISAPIUnsupported(ISAPIError):
    """The panel does not have this endpoint at all (404/405/501/…)."""


class ISAPIClient(ProbeMixin):
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

    # --- plumbing ----------------------------------------------------------
    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._password,)

    async def async_close(self) -> None:
        await self._client.aclose()

    def _authorization(self, method: str, target: str) -> str | None:
        """Header to send up front with the scheme we already know works."""
        if self.auth_mode == AUTH_BASIC:
            return basic_authorization(self._username, self._password)
        if self._challenge is not None:
            self._nc += 1
            return build_authorization(
                self._username, self._password, method, target,
                self._challenge, nc=self._nc,
            )
        return None

    async def _raw(
        self, method: str, url: str, body: str | None, headers: dict[str, str]
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method, url, content=body, headers=headers
            )
        except httpx.HTTPError as err:
            raise ISAPIError(
                f"{method} {url}: {redact(str(err), *self.secrets)}"
            ) from err

    def _record_attempt(self, sent: str | None, status: int) -> None:
        """Keep every attempt, so a basic fallback cannot hide the digest one."""
        scheme = (sent or "").split(" ", 1)[0].lower() or "без заголовка"
        self.last_attempts.append((scheme, safe_auth_header(sent), status))

    def _learn_challenge(self, resp: httpx.Response) -> bool:
        """Remember the digest challenge from a 401. False = none offered."""
        challenges = list(resp.headers.get_list("www-authenticate"))
        self.last_challenge_raw = "; ".join(challenges) or "(заголовок не прислан)"
        if not self.first_challenge_raw:
            self.first_challenge_raw = self.last_challenge_raw
        challenge = pick_digest_challenge(challenges)
        if challenge is None:
            return False
        self._challenge = challenge
        self._nc = 0
        return True

    async def _send(
        self, method: str, path: str, *, body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One request, answering a 401 challenge on the same connection.

        `path` is used verbatim as the digest `uri` (query string included) —
        the panel hashes the request target it received.
        """
        url = f"{self._base}{path}"
        req_headers = dict(headers or {})
        self.last_attempts = []
        sent = self._authorization(method, path)
        if sent:
            req_headers["Authorization"] = sent
        resp = await self._raw(method, url, body, req_headers)
        self.last_auth_header = safe_auth_header(sent)
        self._record_attempt(sent, resp.status_code)
        if resp.status_code != 401:
            if status_ok(resp.status_code):
                self._auth_proven = True
            return resp

        challenges = list(resp.headers.get_list("www-authenticate"))
        self.last_challenge_raw = "; ".join(challenges) or "(заголовок не прислан)"
        if not self.first_challenge_raw:
            self.first_challenge_raw = self.last_challenge_raw
        challenge = pick_digest_challenge(challenges)

        if challenge is not None:
            # Fresh nonce from this very 401, answered immediately so the
            # connection (and the nonce bound to it) is still the same one.
            self._challenge = challenge
            self._nc = 1
            sent = build_authorization(
                self._username, self._password, method, path, challenge, nc=1
            )
            req_headers["Authorization"] = sent
            resp = await self._raw(method, url, body, req_headers)
            self.last_auth_header = safe_auth_header(sent)
            self._record_attempt(sent, resp.status_code)
            if status_ok(resp.status_code):
                if self.auth_mode != AUTH_DIGEST:
                    _LOGGER.info("Панель приняла digest-авторизацию, запоминаем")
                self.auth_mode = AUTH_DIGEST
                self._auth_proven = True
                return resp
            if resp.status_code == 401:
                more = list(resp.headers.get_list("www-authenticate"))
                if more:
                    challenges = more
                    self.last_challenge_raw = "; ".join(more)

        fallback = next_auth_mode(AUTH_DIGEST, 401, "; ".join(challenges))
        if fallback != AUTH_BASIC:
            # Digest was offered and digest failed => normally wrong
            # credentials. Try basic exactly ONCE per client anyway: some
            # firmwares advertise digest and accept only basic, and either
            # way the attempt and its status land in the report.
            if self._auth_proven or self._basic_rescue_tried:
                return resp
            self._basic_rescue_tried = True
            _LOGGER.debug("digest не принят на %s — разовая проверка basic", path)
        if self.auth_mode == AUTH_BASIC:
            return resp

        _LOGGER.debug("401 на %s — пробуем auth=basic", path)
        sent = basic_authorization(self._username, self._password)
        req_headers["Authorization"] = sent
        retry = await self._raw(method, url, body, req_headers)
        self.last_auth_header = safe_auth_header(sent)
        self._record_attempt(sent, retry.status_code)
        if status_ok(retry.status_code):
            _LOGGER.info("Панель приняла basic-авторизацию, запоминаем")
            self.auth_mode = AUTH_BASIC
            self._auth_proven = True
        return retry

    async def _request(self, call: Call) -> httpx.Response:
        """Send a call and raise unless it answered 2xx."""
        resp = await self._send(
            call.method, call.path, body=call.body, headers=call.headers or None
        )
        _LOGGER.debug("ISAPI %s %s -> %s", call.method, call.path, resp.status_code)
        if status_ok(resp.status_code):
            return resp
        if resp.status_code == 401:
            raise ISAPIAuthError(
                f"{call.method} {call.path}: 401 Unauthorized (auth={self.auth_mode})"
            )
        raise ISAPIError(f"{call.method} {call.path}: HTTP {resp.status_code}")

    async def _call_selected(
        self, candidates: tuple[Call, ...], slot: str
    ) -> httpx.Response:
        """Send the remembered call, or find (and remember) one that works.

        Always performs the request — a cached endpoint must never turn
        «открыть дверь» into a no-op. If the cached shape stops working the
        cache is dropped and the candidate list is walked again.
        """
        cached: Call | None = getattr(self, slot, None)
        if cached is not None:
            try:
                return await self._request(cached)
            except ISAPIAuthError:
                raise
            except ISAPIError as err:
                _LOGGER.debug("Эндпоинт %s перестал отвечать (%s)", cached.name, err)
                setattr(self, slot, None)

        auth_failed = False
        response: httpx.Response | None = None

        async def attempt(call: Call) -> bool:
            nonlocal auth_failed, response
            try:
                response = await self._request(call)
            except ISAPIAuthError:
                auth_failed = True
                raise
            return True

        chosen = await select_first_working(candidates, attempt)
        if chosen is None:
            if auth_failed:
                raise ISAPIAuthError("Панель отклонила учётные данные (401)")
            raise ISAPIUnsupported(
                "Ни один из известных эндпоинтов не ответил: "
                + ", ".join(c.path for c in candidates)
            )
        _LOGGER.debug("Выбран эндпоинт %s (%s)", chosen.name, chosen.path)
        setattr(self, slot, chosen)
        assert response is not None
        return response

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
            sent = self._authorization("GET", path)
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
