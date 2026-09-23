"""Transport and authentication for the ISAPI client.

Split out of isapi.py so both stay readable (and under the project's 500-line
rule). `AuthTransportMixin` owns the connection, the digest state machine and
the per-request auth diagnostics; ISAPIClient owns the endpoints.
"""
from __future__ import annotations

import logging

import httpx

from .digest import (
    basic_authorization,
    build_authorization,
    pick_digest_challenge,
    safe_auth_header,
)
from .endpoints import (
    AUTH_BASIC,
    AUTH_DIGEST,
    Call,
    next_auth_mode,
    redact,
    select_first_working,
    status_ok,
)

_LOGGER = logging.getLogger(__name__)


class ISAPIError(Exception):
    """Raised when an ISAPI request fails."""


class ISAPIAuthError(ISAPIError):
    """Credentials rejected by the panel (401 on every auth scheme)."""


class ISAPIUnsupported(ISAPIError):
    """The panel does not have this endpoint at all (404/405/501/…)."""


class ISAPIStatusError(ISAPIError):
    """The panel ANSWERED, with a non-2xx status (as opposed to not answering)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def describe_error(err: BaseException) -> str:
    """Never an empty reason in the log.

    httpx timeouts often stringify to "" — which is exactly how the field log
    ended up saying `Панель недоступна: alertStream:` with nothing after it.
    """
    text = str(err).strip()
    name = type(err).__name__
    return f"{name}: {text}" if text else name


class AuthTransportMixin:
    """HTTP plumbing half of ISAPIClient."""

    # --- plumbing ----------------------------------------------------------
    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._password,)

    async def async_close(self) -> None:
        await self._client.aclose()

    def _authorization(self, method: str, target: str) -> str | None:
        """Header to send up front, or None to ask for a fresh challenge."""
        if self.auth_mode == AUTH_BASIC:
            return basic_authorization(self._username, self._password)
        if self._reuse_nonce and self._challenge is not None:
            self._nc += 1
            return build_authorization(
                self._username, self._password, method, target,
                self._challenge, nc=self._nc,
            )
        # Challenge-per-request: correctness before the saved round trip.
        return None

    def _fresh_digest_header(self, method: str, target: str) -> str | None:
        """Answer the challenge we just learned, with nc=1 and a new cnonce."""
        if self._challenge is None:
            return None
        self._nc = 1
        return build_authorization(
            self._username, self._password, method, target, self._challenge, nc=1
        )

    def _stream_authorization(
        self, method: str, path: str, attempt: int = 0
    ) -> str | None:
        """Auth header for a streaming request.

        Attempt 0 follows the normal policy (nothing, unless basic or the
        opt-in reuse path). Attempt 1 answers the challenge that attempt 0
        just brought back — never an older, already spent one.
        """
        if attempt == 0:
            return self._authorization(method, path)
        return self._fresh_digest_header(method, path)

    def reset_auth(self) -> None:
        """Forget a remembered scheme/nonce and start from digest again.

        A `basic` learned during a broken session must never pin the client:
        digest is proven to work on this hardware.
        """
        self.auth_mode = AUTH_DIGEST
        self._challenge = None
        self._nc = 0
        self._auth_proven = False
        self._basic_rescue_tried = False

    async def _raw(
        self, method: str, url: str, body: str | None, headers: dict[str, str]
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method, url, content=body, headers=headers
            )
        except httpx.HTTPError as err:
            raise ISAPIError(
                f"{method} {url}: {redact(describe_error(err), *self.secrets)}"
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
        if challenge is None and self._challenge is not None:
            # 401 without a challenge after we sent a reused nonce: the panel
            # threw the nonce away. Drop it so the next request asks anew.
            _LOGGER.debug("401 без вызова на %s — забываем nonce", path)
            self._challenge = None

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
                    _LOGGER.info(
                        "Панель приняла digest-авторизацию — возвращаемся на digest"
                    )
                self.auth_mode = AUTH_DIGEST
                self._auth_proven = True
                self._basic_rescue_tried = False
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
        raise ISAPIStatusError(
            f"{call.method} {call.path}: HTTP {resp.status_code}", resp.status_code
        )

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
            except ISAPIStatusError as err:
                # The panel answered, differently: re-discover the shape.
                _LOGGER.debug("Эндпоинт %s перестал отвечать (%s)", cached.name, err)
                setattr(self, slot, None)
            # ISAPIAuthError and transport errors (panel offline) propagate
            # untouched: the learned shape is still right, the panel is not.

        auth_failed = False
        offline: ISAPIError | None = None
        response: httpx.Response | None = None

        async def attempt(call: Call) -> bool:
            nonlocal auth_failed, offline, response
            if offline is not None:
                # The panel did not answer at all: other endpoint SHAPES
                # cannot help, so do not knock on the door three times.
                return False
            try:
                response = await self._request(call)
            except ISAPIAuthError:
                auth_failed = True
                raise
            except ISAPIStatusError:
                raise
            except ISAPIError as err:
                offline = err
                raise
            return True

        chosen = await select_first_working(candidates, attempt)
        if chosen is None:
            if auth_failed:
                raise ISAPIAuthError("Панель отклонила учётные данные (401)")
            if offline is not None:
                # Not an answer at all (timeout, refused, reset): retryable.
                # Concluding "unsupported" here would switch the doorbell off
                # for good after a power blip at the gate.
                raise ISAPIError(f"панель не отвечает: {offline}")
            raise ISAPIUnsupported(
                "Ни один из известных эндпоинтов не ответил: "
                + ", ".join(c.path for c in candidates)
            )
        _LOGGER.debug("Выбран эндпоинт %s (%s)", chosen.name, chosen.path)
        setattr(self, slot, chosen)
        assert response is not None
        return response
