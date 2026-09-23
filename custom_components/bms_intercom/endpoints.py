"""Endpoint tables and pure selection logic for Hikvision ISAPI panels.

Hikvision firmwares disagree about paths: a DS-KV door station, a DS-K1T3xx
access terminal and an old DS-KD panel each answer on a slightly different
set. Instead of hard-coding one shape we keep an ordered list of candidates
per capability and let the client pick the first one that actually answers.

Everything in this module is pure (no I/O, no httpx, no Home Assistant) so it
can be unit-tested on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence, TypeVar

# Main stream of a Hikvision channel. 101 = channel 1 main stream,
# 102 = channel 1 sub stream, 1 = the pre-ISAPI shape some old units use.
DEFAULT_CHANNEL = 101
FALLBACK_CHANNELS: tuple[int, ...] = (101, 102, 1)

DOOR_OPEN_XML = "<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>"
DOOR_OPEN_JSON = '{"RemoteControlDoor": {"cmd": "open"}}'

XML_CT = "application/xml"
JSON_CT = "application/json"


@dataclass(frozen=True)
class Call:
    """One candidate ISAPI request."""

    name: str
    method: str
    path: str
    body: str | None = None
    content_type: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": self.content_type} if self.content_type else {}


# --- channels / streams ----------------------------------------------------
def channel_order(channel: int | None) -> tuple[int, ...]:
    """Configured channel first, then the other known shapes."""
    order: list[int] = []
    if channel:
        order.append(int(channel))
    order.extend(c for c in FALLBACK_CHANNELS if c not in order)
    return tuple(order)


def rtsp_paths(channel: int | None = DEFAULT_CHANNEL) -> tuple[str, ...]:
    """RTSP stream paths to try, best first.

    `/Streaming/Channels/101` is what DS-K1T341AM speaks; `/1` (the shape the
    stock hikvision integration builds) is kept last as a fallback because it
    answers 400 Bad Request on this firmware.
    """
    return tuple(f"/Streaming/Channels/{c}" for c in channel_order(channel))


def snapshot_paths(channel: int | None = DEFAULT_CHANNEL) -> tuple[str, ...]:
    """Still-image paths to try, best first."""
    paths: list[str] = []
    for c in channel_order(channel):
        paths.append(f"/ISAPI/Streaming/channels/{c}/picture")
    for c in channel_order(channel):
        paths.append(f"/ISAPI/Streaming/channels/{c}/httpPreview")
    paths.append(f"/Streaming/channels/{channel_order(channel)[0]}/picture")
    return tuple(paths)


def stream_info_paths(channel: int | None = DEFAULT_CHANNEL) -> tuple[str, ...]:
    """ISAPI descriptors used to check that a channel exists at all."""
    return tuple(
        f"/ISAPI/Streaming/channels/{c}" for c in channel_order(channel)
    )


def rtsp_url(
    host: str,
    port: int,
    username: str,
    password: str,
    path: str,
    *,
    redacted: bool = False,
) -> str:
    """Build an RTSP URL; `redacted=True` never carries the credentials."""
    from urllib.parse import quote

    if redacted:
        return f"rtsp://***@{host}:{port}{path}"
    user = quote(username or "", safe="")
    pwd = quote(password or "", safe="")
    return f"rtsp://{user}:{pwd}@{host}:{port}{path}"


# --- ISAPI calls -----------------------------------------------------------
def identity_calls() -> tuple[Call, ...]:
    """Reachability / credentials check."""
    return (
        # userCheck is the endpoint a browser was proven on for DS-K1T341AM.
        Call("userCheck", "GET", "/ISAPI/Security/userCheck"),
        Call("deviceInfo", "GET", "/ISAPI/System/deviceInfo"),
        Call("deviceInfo.json", "GET", "/ISAPI/System/deviceInfo?format=json"),
        Call("systemStatus", "GET", "/ISAPI/System/status"),
    )


def door_calls(door_no: int = 1) -> tuple[Call, ...]:
    """Door-relay open commands, best first."""
    base = f"/ISAPI/AccessControl/RemoteControl/door/{door_no}"
    return (
        Call("door.xml", "PUT", base, DOOR_OPEN_XML, XML_CT),
        Call("door.json", "PUT", f"{base}?format=json", DOOR_OPEN_JSON, JSON_CT),
        Call(
            "door.videoIntercom",
            "PUT",
            f"/ISAPI/VideoIntercom/unlock/{door_no}?format=json",
            '{"Unlock": {"unlockType": "remoteUnlock"}}',
            JSON_CT,
        ),
    )


def call_status_calls() -> tuple[Call, ...]:
    """Polling fallbacks for the call state (used when alertStream is off)."""
    return (
        Call("callStatus.json", "GET", "/ISAPI/VideoIntercom/callStatus?format=json"),
        Call("callStatus.xml", "GET", "/ISAPI/VideoIntercom/callStatus"),
        Call("callSignal.json", "GET", "/ISAPI/VideoIntercom/callSignal?format=json"),
    )


def call_signal_calls(cmd: str) -> tuple[Call, ...]:
    """Answer / reject. Many access terminals have neither — see tolerate()."""
    return (
        Call(
            f"callSignal.{cmd}.json",
            "PUT",
            "/ISAPI/VideoIntercom/callSignal?format=json",
            '{"CallSignal": {"cmdType": "%s"}}' % cmd,
            JSON_CT,
        ),
        Call(
            f"callSignal.{cmd}.xml",
            "PUT",
            "/ISAPI/VideoIntercom/callSignal",
            f"<CallSignal><cmdType>{cmd}</cmdType></CallSignal>",
            XML_CT,
        ),
    )


#: Capability document; tells us whether answer/reject exist on this model.
VIDEO_INTERCOM_CAPABILITIES = "/ISAPI/VideoIntercom/capabilities?format=json"


def capability_calls() -> tuple[Call, ...]:
    return (
        Call("videoIntercom.capabilities", "GET", VIDEO_INTERCOM_CAPABILITIES),
        Call("videoIntercom.capabilities.xml", "GET", "/ISAPI/VideoIntercom/capabilities"),
    )


ALERT_STREAM_PATH = "/ISAPI/Event/notification/alertStream"


def alert_stream_paths() -> tuple[str, ...]:
    return (ALERT_STREAM_PATH, "/ISAPI/Event/notification/alertStream?format=json")


# --- status handling -------------------------------------------------------
def status_ok(status: int | None) -> bool:
    """True when the endpoint answered successfully."""
    return status is not None and 200 <= status < 300


#: The panel says "no such endpoint here" — try the next candidate.
_MISSING = (400, 403, 404, 405, 406, 415, 501)


def status_missing(status: int | None) -> bool:
    """True when this endpoint shape is simply not supported by the model."""
    return status in _MISSING


def tolerate(status: int | None) -> bool:
    """Should a failing optional command be swallowed instead of raised?

    Access terminals like the DS-K1T341AM have no answer/reject endpoint at
    all; a missing endpoint must never make the entity unavailable.
    """
    return status_missing(status)


T = TypeVar("T")


async def select_first_working(
    candidates: Sequence[T],
    attempt: Callable[[T], Awaitable[bool]],
) -> T | None:
    """Return the first candidate whose `attempt` says it works.

    Exceptions from `attempt` count as "does not work" — the next candidate is
    tried. Returns None when nothing answered.
    """
    for candidate in candidates:
        try:
            if await attempt(candidate):
                return candidate
        except Exception:  # noqa: BLE001 - a broken candidate is just skipped
            continue
    return None


# --- auth ------------------------------------------------------------------
AUTH_DIGEST = "digest"
AUTH_BASIC = "basic"


def next_auth_mode(
    current: str, status: int | None, www_authenticate: str | None
) -> str | None:
    """Which auth scheme to retry a 401 with, or None to give up.

    Hikvision defaults to digest. Some units (and some firmware options) only
    accept basic, in which case the 401 advertises Basic — or advertises
    nothing at all, and we still try basic once before giving up.
    """
    if status != 401:
        return None
    header = (www_authenticate or "").lower()
    if current == AUTH_DIGEST:
        if "basic" in header:
            return AUTH_BASIC
        if "digest" in header:
            # Digest offered and digest already failed => wrong credentials.
            return None
        return AUTH_BASIC
    return None


# --- redaction -------------------------------------------------------------
_CRED_IN_URL = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)(?:[^/\s:@]{1,128}:[^/\s@]{1,256}@)"
)
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)\S+")
# httpHosts and similar documents can carry the push target's credentials.
_XML_SECRET = re.compile(
    r"(?is)(<(?:[\w-]+:)?(password|passwd|secretKey|key)\b[^>]*>)(.*?)(</(?:[\w-]+:)?\2\s*>)"
)
_JSON_SECRET = re.compile(
    r'(?i)("(?:password|passwd|secretKey)"\s*:\s*)"(?:[^"\\]|\\.)*"'
)


def redact(text: str | None, *secrets: str | None) -> str:
    """Strip credentials from anything that goes to the log or the UI."""
    if not text:
        return ""
    out = _CRED_IN_URL.sub(r"\1***@", text)
    out = _AUTH_HEADER.sub(r"\1***", out)
    out = _XML_SECRET.sub(lambda m: f"{m.group(1)}***{m.group(4)}", out)
    out = _JSON_SECRET.sub(r'\1"***"', out)
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, "***")
    return out


def snippet(body: str | bytes | None, *, limit: int = 120, secrets: Iterable[str | None] = ()) -> str:
    """First `limit` characters of a body, on one line, without secrets."""
    if body is None:
        return ""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    text = redact(text, *secrets)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text
