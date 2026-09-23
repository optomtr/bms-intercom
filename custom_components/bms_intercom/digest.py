"""Hand-rolled HTTP digest auth (RFC 2069 / 2617 / 7616).

Why not `httpx.DigestAuth`: Hikvision V3.x ISAPI parses the Authorization
header with a very literal parser. httpx always emits `algorithm=…` even when
the challenge never offered one, and orders the fields differently from a
browser — and a browser is what we proved works on this panel
(`/ISAPI/Security/userCheck` in Chrome). So we build the header ourselves, in
the browser's field order, echoing back only what the challenge asked for.

Pure module: no I/O, no httpx, no Home Assistant — unit-testable on its own.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field

# Algorithms a Hikvision panel may ask for.
_HASHES = {
    "MD5": hashlib.md5,
    "MD5-SESS": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-256-SESS": hashlib.sha256,
    "SHA-512-256": lambda data: hashlib.sha512(data),  # truncated below
    "SHA-512-256-SESS": lambda data: hashlib.sha512(data),
}

_TOKEN = re.compile(
    r'(?P<key>[A-Za-z0-9_\-]+)\s*=\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^,\s]*))'
)


@dataclass
class Challenge:
    """A parsed `WWW-Authenticate: Digest …` challenge."""

    realm: str = ""
    nonce: str = ""
    qop: str = ""
    algorithm: str = ""      # empty == the panel did not ask for one
    opaque: str = ""
    stale: str = ""
    raw: str = ""
    params: dict[str, str] = field(default_factory=dict)

    @property
    def hash_name(self) -> str:
        name = (self.algorithm or "MD5").upper()
        return name if name in _HASHES else "MD5"

    def describe(self) -> str:
        """One line for the diagnostics report (carries no secret)."""
        return (
            f"realm={self.realm!r} qop={self.qop or '—'} "
            f"algorithm={self.algorithm or '(не указан)'} "
            f"nonce={len(self.nonce)} симв. "
            f"opaque={'да' if self.opaque else 'нет'}"
            + (f" stale={self.stale}" if self.stale else "")
        )


def parse_challenge(header: str | None) -> Challenge | None:
    """Parse one `WWW-Authenticate` value; None when it is not digest."""
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "digest":
        return None
    params: dict[str, str] = {}
    for match in _TOKEN.finditer(rest):
        value = match.group("quoted")
        if value is None:
            value = match.group("bare") or ""
        params[match.group("key").lower()] = value
    return Challenge(
        realm=params.get("realm", ""),
        nonce=params.get("nonce", ""),
        qop=params.get("qop", ""),
        algorithm=params.get("algorithm", ""),
        opaque=params.get("opaque", ""),
        stale=params.get("stale", ""),
        raw=header.strip(),
        params=params,
    )


def pick_digest_challenge(headers: list[str]) -> Challenge | None:
    """First digest challenge among possibly several WWW-Authenticate values."""
    for header in headers:
        challenge = parse_challenge(header)
        if challenge is not None:
            return challenge
    return None


def _hash(name: str, data: str) -> str:
    func = _HASHES.get(name, hashlib.md5)
    digest = func(data.encode("utf-8"))
    hexed = digest.hexdigest()
    if name.startswith("SHA-512-256"):
        return hexed[:64]
    return hexed


def make_cnonce() -> str:
    seed = f"{time.time()}{os.urandom(8)!r}".encode()
    return hashlib.sha1(seed).hexdigest()[:16]


def choose_qop(offered: str) -> str:
    """`auth` when offered (possibly among others), else '' (RFC 2069 mode).

    auth-int would need the body hashed; no Hikvision firmware asks for it,
    and answering `auth-int` with an `auth` response would be worse than
    falling back to the RFC 2069 shape.
    """
    if not offered:
        return ""
    values = [v.strip().lower() for v in re.split(r"[,\s]+", offered) if v.strip()]
    return "auth" if "auth" in values else ""


def build_authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: Challenge,
    *,
    nc: int = 1,
    cnonce: str | None = None,
) -> str:
    """Build the `Authorization: Digest …` value.

    `uri` must be exactly the request target the panel sees, query string
    included — Hikvision hashes what it received, not a normalised path.
    """
    name = challenge.hash_name
    sess = name.endswith("-SESS")
    cnonce = cnonce or make_cnonce()
    nc_value = "%08x" % nc
    qop = choose_qop(challenge.qop)

    ha1 = _hash(name, f"{username}:{challenge.realm}:{password}")
    if sess:
        ha1 = _hash(name, f"{ha1}:{challenge.nonce}:{cnonce}")
    ha2 = _hash(name, f"{method.upper()}:{uri}")

    if qop:
        response = _hash(
            name, f"{ha1}:{challenge.nonce}:{nc_value}:{cnonce}:{qop}:{ha2}"
        )
    else:
        response = _hash(name, f"{ha1}:{challenge.nonce}:{ha2}")

    # Browser field order. `algorithm` is echoed back only when the panel
    # offered it — firmwares that never mention it can choke on an extra field.
    parts = [
        f'username="{username}"',
        f'realm="{challenge.realm}"',
        f'nonce="{challenge.nonce}"',
        f'uri="{uri}"',
    ]
    if challenge.algorithm:
        parts.append(f"algorithm={challenge.algorithm}")
    parts.append(f'response="{response}"')
    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')
    if qop:
        parts.append(f"qop={qop}")
        parts.append(f"nc={nc_value}")
        parts.append(f'cnonce="{cnonce}"')
    return "Digest " + ", ".join(parts)


def basic_authorization(username: str, password: str) -> str:
    from base64 import b64encode

    token = b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def safe_auth_header(header: str | None) -> str:
    """Diagnostics-safe form of an Authorization header.

    A digest header carries no secret — username, nonce, uri and the response
    hash are exactly what we need to see, so it is shown verbatim. A basic
    header is the password in base64, so only the scheme survives.
    """
    if not header:
        return ""
    scheme = header.split(" ", 1)[0].lower()
    if scheme == "digest":
        return header
    return f"{header.split(' ', 1)[0]} ***"
