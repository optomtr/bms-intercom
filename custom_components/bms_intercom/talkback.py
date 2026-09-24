"""Микрофон оператора → панель: native ISAPI two-way audio.

Перенесено из isapi.py форка (Abdunazar7/bms-intercom, коммит b9f707f) в
отдельный модуль: вместе с нашим ISAPI-клиентом isapi.py перевалил бы за
правило репозитория в 500 строк.

Как это работает (повторяет isapi-клиент go2rtc, проверено на DS-KV6113):
найти канал two-way audio, закрыть/открыть его, затем держать сырой сокет к
`.../audioData` и писать в него байты G.711. Запрос audioData уходит с
Content-Length: 0, а звук потом льётся по тому же соединению — нестандартная
схема Hikvision, поэтому здесь сырой сокет, а не httpx.

ИЗВЕСТНЫЙ ДОЛГ: у этого модуля пока СВОЙ digest (`_parse_digest_challenge`,
`_digest_header`) и httpx.DigestAuth для управляющих запросов — как в форке.
Общая логика digest проекта живёт в digest.py (одноразовый nonce, эхо пустого
opaque); перевод аудио на неё отложен по решению владельца (срочность),
до проверки на живой панели.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re

import httpx

_LOGGER = logging.getLogger(__name__)

#: Предел одного куска звука в base64 от браузера. Обычный кусок ~1 КБ
#: (4096 сэмплов → 8 кГц G.711); больше — это уже не микрофон, а злоупотребление
#: WebSocket-командой, и до панели он не должен дойти.
MAX_TALK_B64 = 65536


def decode_talk_chunk(data: object) -> bytes:
    """base64 от браузера → байты G.711, или b"" если кусок не годится.

    Пустой результат = ничего не слать панели: мусорный base64, не строка,
    слишком большой кусок. validate=True — иначе b64decode молча выкидывает
    посторонние символы и отдаёт панели обрезки.
    """
    if not isinstance(data, str) or not data or len(data) > MAX_TALK_B64:
        return b""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return b""


# --- мелкие помощники ---------------------------------------------------------
def between(s: str, a: str, b: str) -> str | None:
    """Подстрока между первым `a` и следующим за ним `b` (грубый разбор XML)."""
    i = s.find(a)
    if i < 0:
        return None
    i += len(a)
    j = s.find(b, i)
    return s[i:j] if j >= 0 else None


def _parse_digest_challenge(header: str) -> dict[str, str]:
    """Разобрать `WWW-Authenticate: Digest ...` в словарь (digest форка)."""
    params: dict[str, str] = {}
    h = header.split(" ", 1)[1] if " " in header else header
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]+))', h):
        params[m.group(1).lower()] = (
            m.group(2) if m.group(2) is not None else (m.group(3) or "").strip()
        )
    return params


def _digest_header(method: str, uri: str, chal: dict[str, str], user: str, pwd: str) -> str:
    """Заголовок `Authorization: Digest ...` для сырого сокета (digest форка)."""
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    qop = chal.get("qop")
    opaque = chal.get("opaque")
    ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        nc = "00000001"
        cnonce = os.urandom(8).hex()
        resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        out = (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'
        )
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        out = (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"'
        )
    if opaque:
        out += f', opaque="{opaque}"'
    return out


async def _read_http_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    """Прочитать HTTP-ответ до конца заголовков; вернуть (статус, заголовки)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ") if lines else []
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


class TwoWayAudioError(Exception):
    """Сбой ISAPI two-way audio (сокет не открылся или оборвался)."""


class TwoWayAudioSession:
    """Шлёт сырой G.711 на панель через ISAPI two-way audio."""

    def __init__(self, host: str, http_port: int, username: str, password: str) -> None:
        self._host = host
        self._port = http_port
        self._user = username
        self._pass = password
        self._channel = "1"
        self.codec = "G.711ulaw"
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        # verify=False: не грузить certifi в цикле событий — с панелью всё
        # равно говорим по голому HTTP.
        self._client = httpx.AsyncClient(timeout=10.0, verify=False)

    async def _req(self, method: str, path: str):
        auth = httpx.DigestAuth(self._user, self._pass)
        return await self._client.request(
            method, f"http://{self._host}:{self._port}{path}", auth=auth
        )

    async def async_open(self) -> None:
        """Найти канал, (пере)открыть его и открыть сокет audioData."""
        resp = await self._req("GET", "/ISAPI/System/TwoWayAudio/channels")
        xml = resp.text
        self._channel = between(xml, "<id>", "<") or "1"
        self.codec = between(xml, "<audioCompressionType>", "<") or "G.711ulaw"

        base = f"/ISAPI/System/TwoWayAudio/channels/{self._channel}"
        # Зависшая прошлая сессия не даёт открыть новую; close на простаивающем
        # канале безвреден.
        try:
            await self._req("PUT", base + "/close")
        except httpx.HTTPError:
            pass
        await self._req("PUT", base + "/open")

        self._writer = await self._open_audio_socket(base + "/audioData")
        _LOGGER.debug(
            "ISAPI two-way audio открыт: канал %s, кодек %s", self._channel, self.codec
        )

    async def _open_audio_socket(self, path: str) -> asyncio.StreamWriter:
        host, port = self._host, self._port
        body_head = (
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: 0\r\n\r\n"
        )

        def request(auth: str | None) -> bytes:
            h = f"PUT {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if auth:
                h += f"Authorization: {auth}\r\n"
            return (h + body_head).encode()

        # Тайм-ауты: панель, которая не отвечает, не должна подвесить talk_start.
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
        writer.write(request(None))
        await writer.drain()
        status, headers = await asyncio.wait_for(_read_http_head(reader), 10)

        if status == 401:
            chal = _parse_digest_challenge(headers.get("www-authenticate", ""))
            auth = _digest_header("PUT", path, chal, self._user, self._pass)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
            writer.write(request(auth))
            await writer.drain()
            status, headers = await asyncio.wait_for(_read_http_head(reader), 10)

        if status != 200:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            raise TwoWayAudioError(f"audioData HTTP {status}")
        return writer

    async def async_send(self, data: bytes) -> None:
        """Записать кусок G.711 в сокет панели."""
        if self._writer is None:
            return
        async with self._lock:
            try:
                self._writer.write(data)
                await asyncio.wait_for(self._writer.drain(), 5)
            except Exception as err:  # noqa: BLE001
                raise TwoWayAudioError(str(err)) from err

    async def async_close(self) -> None:
        """Прекратить отправку и закрыть канал two-way audio."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        try:
            await self._req("PUT", f"/ISAPI/System/TwoWayAudio/channels/{self._channel}/close")
        except httpx.HTTPError:
            pass
        await self._client.aclose()
