"""Panel self-diagnostics: probe every candidate endpoint, report safely.

Split out of isapi.py to keep both files readable (and under the project's
500-line rule). `ProbeMixin` is mixed into `ISAPIClient` and uses its plumbing
(`_send`, `_authorization`, `_client`, the learned endpoint shapes).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .digest import (
    basic_authorization,
    build_authorization,
    pick_digest_challenge,
    safe_auth_header,
)
from .endpoints import (
    alert_stream_paths,
    call_status_calls,
    channel_order,
    door_calls,
    identity_calls,
    redact,
    rtsp_paths,
    rtsp_url,
    snapshot_paths,
    snippet,
    status_missing,
    status_ok,
    stream_info_paths,
)
from .events import parse_document
from .probe import ProbeResult

_LOGGER = logging.getLogger(__name__)


#: The endpoint a browser was proven to authenticate against on DS-K1T341AM.
AUTH_CHECK_PATH = "/ISAPI/Security/userCheck"


def _verdict(value: bool | None, yes: str, no: str) -> str:
    """Tri-state wording for the report."""
    if value is None:
        return "не установлено"
    return yes if value else no


class ProbeMixin:
    """Diagnostics half of ISAPIClient."""

    @staticmethod
    def _door_verdict(results: list[ProbeResult]) -> str:
        """What the opt-in door PUT actually answered."""
        door = [r for r in results if r.name.startswith("door.") and r.method == "PUT"]
        if not door:
            return "команда открытия не отправлялась"
        ok = [r for r in door if r.ok]
        if ok:
            return f"ОТКРЫТА командой {ok[0].name} (HTTP {ok[0].status})"
        codes = ", ".join(f"{r.name} → {r.status}" for r in door)
        return f"не открылась: {codes}"

    async def async_auth_matrix(self, path: str = AUTH_CHECK_PATH) -> dict[str, Any]:
        """Try digest and basic explicitly, side by side, on one endpoint.

        Neither attempt touches the client's remembered scheme, and neither
        can hide the other: the report gets `digest → <status>` and
        `basic → <status>` for the very endpoint the browser succeeded on.
        """
        url = f"{self._base}{path}"
        out: dict[str, Any] = {
            "путь": path,
            "digest": "не пробовали",
            "basic": "не пробовали",
            "digest_header": "",
            "challenge": "",
        }

        try:
            bare = await self._raw("GET", url, None, {})
        except Exception as err:  # noqa: BLE001 - diagnostics never raise
            out["digest"] = f"ошибка: {err}"
            return out

        if bare.status_code != 401:
            out["digest"] = f"{bare.status_code} (401 не пришёл)"
        else:
            challenges = list(bare.headers.get_list("www-authenticate"))
            out["challenge"] = "; ".join(challenges) or "(заголовок не прислан)"
            challenge = pick_digest_challenge(challenges)
            if challenge is None:
                out["digest"] = "панель не предлагает digest"
            else:
                header = build_authorization(
                    self._username, self._password, "GET", path, challenge, nc=1
                )
                out["digest_header"] = header
                try:
                    signed = await self._raw("GET", url, None, {"Authorization": header})
                    out["digest"] = signed.status_code
                except Exception as err:  # noqa: BLE001
                    out["digest"] = f"ошибка: {err}"

        try:
            basic = await self._raw(
                "GET", url, None,
                {"Authorization": basic_authorization(self._username, self._password)},
            )
            out["basic"] = basic.status_code
        except Exception as err:  # noqa: BLE001
            out["basic"] = f"ошибка: {err}"
        return out

    async def _probe_one(
        self, name: str, method: str, path: str, *, body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProbeResult:
        result = ProbeResult(
            name=name, method=method, url=f"{self._base}{path}", secrets=self.secrets
        )
        try:
            resp = await self._send(method, path, body=body, headers=headers)
        except ISAPIError as err:
            result.error = str(err)
            return result
        result.status = resp.status_code
        result.auth = self.auth_mode
        result.sent_auth = self.last_auth_header
        result.attempts = tuple(self.last_attempts)
        if resp.status_code == 401:
            result.challenge = self.last_challenge_raw
        result.content_type = resp.headers.get("content-type", "")
        if result.content_type.startswith(("image/", "video/", "application/octet")):
            result.body = f"[{len(resp.content)} байт двоичных данных]"
        else:
            result.body = resp.text
        return result

    async def async_probe(self, *, test_door: bool = False) -> tuple[list[ProbeResult], dict[str, Any]]:
        """Probe every candidate endpoint and return results + a summary.

        The door relay is NOT triggered unless `test_door=True` — a diagnostic
        must not unlock the entrance.
        """
        results: list[ProbeResult] = []

        for call in identity_calls():
            results.append(await self._probe_one(call.name, call.method, call.path))
        for path in stream_info_paths(self._channel):
            channel = path.rsplit("/", 1)[-1]
            results.append(await self._probe_one(f"channel {channel}", "GET", path))
            # First channel the panel confirms is the one the RTSP URL uses.
            if results[-1].ok and self._rtsp_path is None:
                self._rtsp_path = f"/Streaming/Channels/{channel}"
        for path in snapshot_paths(self._channel)[:4]:
            results.append(await self._probe_one("snapshot", "GET", path))
        for call in call_status_calls():
            results.append(await self._probe_one(call.name, call.method, call.path))
        results.append(
            await self._probe_one(
                "videoIntercom.capabilities", "GET",
                "/ISAPI/VideoIntercom/capabilities?format=json",
            )
        )
        results.append(
            await self._probe_one(
                "door.capabilities", "GET",
                f"/ISAPI/AccessControl/RemoteControl/door/{self._door_no}/capabilities?format=json",
            )
        )
        stream_result = await self._probe_alert_stream()
        results.append(stream_result)
        if stream_result.ok:
            self.alert_stream_supported = True
        elif status_missing(stream_result.status):
            self.alert_stream_supported = False
        # The catalogue answers double as capability detection.
        for result in results:
            if "capabilities" in result.name and "videoIntercom" in result.name:
                self.capabilities_raw = result.body
                body = result.body.lower()
                if result.ok:
                    self.call_signal_supported = (
                        "callsignal" in body or "cmdtype" in body
                    )
            if result.name == "snapshot" and result.ok:
                self.snapshot_supported = True
        if self.snapshot_supported is None and not any(
            r.ok for r in results if r.name == "snapshot"
        ):
            self.snapshot_supported = False
        if self.call_signal_supported is None:
            signal = [r for r in results if r.name.startswith("callSignal")]
            if signal and not any(r.ok for r in signal):
                self.call_signal_supported = False
        if test_door:
            for call in door_calls(self._door_no):
                results.append(
                    await self._probe_one(
                        call.name, call.method, call.path,
                        body=call.body, headers=call.headers or None,
                    )
                )
                if status_ok(results[-1].status):
                    break

        matrix = await self.async_auth_matrix()
        working = [r for r in results if r.ok]
        summary: dict[str, Any] = {
            "модель": self._device_model(results),
            "WWW-Authenticate (первый 401)": self.first_challenge_raw or "(401 не было)",
            "разбор вызова": (
                self._challenge.describe() if self._challenge else "(digest не предложен)"
            ),
            f"проверка {matrix['путь']}": (
                f"digest → {matrix['digest']}, basic → {matrix['basic']}"
            ),
            "Authorization (digest, эта проверка)": (
                matrix["digest_header"] or "(digest не отправляли)"
            ),
            "авторизация": (
                f"{self.auth_mode} (успешно)" if working else f"{self.auth_mode} (ни один запрос не прошёл)"
            ),
            "RTSP": rtsp_url(
                self._host, self._rtsp_port, self._username, self._password,
                self._rtsp_path or rtsp_paths(self._channel)[0], redacted=True,
            ),
            "каналы (порядок проверки)": ", ".join(str(c) for c in channel_order(self._channel)),
            "успешно": f"{len(working)} из {len(results)}",
            "поток событий (alertStream)": _verdict(
                self.alert_stream_supported, "есть", "нет — работаем опросом callStatus"
            ),
            "снимок по ISAPI": _verdict(
                self.snapshot_supported, "есть", "нет — кадры из RTSP-потока"
            ),
            "ответить/сбросить": _verdict(
                self.call_signal_supported,
                "есть",
                "нет — кнопки меняют только состояние в HA",
            ),
            "реле двери": (
                self._door_verdict(results) if test_door
                else "не трогали (test_door=false)"
            ),
        }
        return results, summary

    async def _probe_alert_stream(self) -> ProbeResult:
        """Open alertStream briefly and report what the first bytes look like."""
        path = alert_stream_paths()[0]
        result = ProbeResult(
            name="alertStream", method="GET", url=f"{self._base}{path}",
            secrets=self.secrets,
        )
        try:
            # Attempt 0 may come back 401; attempt 1 answers that challenge.
            for attempt in (0, 1):
                sent = self._stream_authorization("GET", path, attempt)
                headers = {"Authorization": sent} if sent else {}
                async with self._client.stream(
                    "GET", f"{self._base}{path}",
                    headers=headers,
                    timeout=httpx.Timeout(10.0, read=8.0),
                ) as resp:
                    result.status = resp.status_code
                    result.auth = self.auth_mode
                    result.sent_auth = safe_auth_header(sent)
                    result.attempts += (
                        (
                            (sent or "").split(" ", 1)[0].lower() or "без заголовка",
                            safe_auth_header(sent),
                            resp.status_code,
                        ),
                    )
                    result.content_type = resp.headers.get("content-type", "")

                    if resp.status_code == 401:
                        await resp.aread()
                        result.challenge = "; ".join(
                            resp.headers.get_list("www-authenticate")
                        ) or "(заголовок не прислан)"
                        if attempt == 0 and self._learn_challenge(resp):
                            continue
                        result.body = ""
                        return result

                    if not status_ok(resp.status_code):
                        result.body = ""
                        return result

                    try:
                        async for chunk in resp.aiter_bytes():
                            result.body = snippet(
                                chunk, limit=400, secrets=self.secrets
                            )
                            if result.body:
                                break
                    except httpx.ReadTimeout:
                        result.body = "(подключение живо, событий пока нет)"
                    if not result.body:
                        result.body = "(подключение живо, событий пока нет)"
                    return result
        except httpx.HTTPError as err:
            result.error = redact(str(err), *self.secrets)
        return result

    @staticmethod
    def _device_model(results: list[ProbeResult]) -> str:
        for result in results:
            if result.ok and "deviceInfo" in result.name:
                event = parse_document(result.body) or {}
                fields = event.get("fields", {})
                model = fields.get("model", "")
                firmware = fields.get("firmwareversion", "")
                if model:
                    return f"{model} {firmware}".strip()
        return "не определена"
