"""Built-in local HTTPS endpoint for BMS Intercom.

To use the browser microphone (two-way audio) the Home Assistant page must be
opened over a secure context (HTTPS). Instead of asking the user to set up an
add-on, this module starts — automatically, inside the integration — a small
HTTPS reverse proxy on a separate port that forwards everything (pages,
WebSocket, camera streams) to Home Assistant on 127.0.0.1 — on the port HA
itself listens on (see backend_urls).

The user configures nothing: a self-signed certificate is generated on first
run (SAN covers all local IPs + hostnames). The only unavoidable step is the
browser's one-time "trust self-signed certificate" prompt.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import os
import ssl

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

_LOGGER = logging.getLogger(__name__)

#: Последний запасной порт — только если HA не сказал свой (не должно быть).
_DEFAULT_HA_PORT = 8123


def backend_urls(hass) -> tuple[str, str]:
    """Адрес HA для прокси: (http-база, ws-база) на 127.0.0.1.

    Почему не зашитый 8123: на объекте HA слушает порт 80 (server_port в
    `http:`), 8123 закрыт — прокси отвечал 502 на всё. Порт берём у самого HA:
    hass.http.server_port (ставится при setup компонента http — он у нас в
    dependencies, значит к старту интеграции уже есть), запасной —
    hass.config.api.port. Если в `http:` задан ssl_certificate, HA на этом
    порту говорит только TLS — тогда https/wss (проверку сертификата
    отключает _BACKEND_SSL: свой же HA на loopback, сертификат выписан на имя).
    """
    http = getattr(hass, "http", None)
    api = getattr(getattr(hass, "config", None), "api", None)
    port = getattr(http, "server_port", None) or getattr(api, "port", None)
    if not isinstance(port, int) or not 0 < port < 65536:
        port = _DEFAULT_HA_PORT
    tls = bool(getattr(http, "ssl_certificate", None) or getattr(api, "use_ssl", False))
    host = f"127.0.0.1:{port}"
    return (f"https://{host}", f"wss://{host}") if tls else (f"http://{host}", f"ws://{host}")


#: aiohttp: False = не проверять сертификат (на http-адрес не влияет).
_BACKEND_SSL = False

# Hop-by-hop headers must not be forwarded.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

# Заголовки пересылки. Прокси их вырезает (анти-спуф из форка: клиент не
# должен выдавать себя за другой IP) и НЕ ставит сам.
#
# Почему не ставит: Home Assistant (components/http/forwarded.py) отвечает
# 400 на ЛЮБОЙ запрос с X-Forwarded-For, если в `http:` не включены
# use_x_forwarded_for и trusted_proxies со 127.0.0.1 — а по умолчанию они
# выключены. Надёжно узнать эти настройки из интеграции нельзя
# (use_x_forwarded_for не хранится на hass.http), поэтому HA видит прокси как
# localhost — так и задумано с bed2583, trusted_proxies не нужен. Без XFF ядро
# HA игнорирует и X-Forwarded-Proto/Host, так что их тоже не ставим.
_FORWARD_STRIP = {
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded",
    "x-real-ip",
}

# HTTP: не пересылаем hop-by-hop, Host (aiohttp поставит свой) и пересылочные.
_HTTP_SKIP = _HOP | _FORWARD_STRIP | {"host"}

# WebSocket: рукопожатие aiohttp строит сам — его заголовки тоже не шлём.
# Всё остальное (cookies и т.п.) пересылается так же, как в HTTP: иначе сессия
# Ingress аддонов (Music Assistant и пр.) в WS не проходит (фикс форка 6bbbdad).
_WS_SKIP = _HTTP_SKIP | {
    "sec-websocket-key", "sec-websocket-version",
    "sec-websocket-extensions", "sec-websocket-protocol",
}


def _forward_headers(request: web.Request, skip: set[str]) -> CIMultiDict[str]:
    """Заголовки клиента для запроса к HA — один путь для HTTP и WS.

    CIMultiDict + add() сохраняет повторяющиеся заголовки (несколько Cookie).
    """
    headers: CIMultiDict[str] = CIMultiDict()
    for k, v in request.headers.items():
        if k.lower() not in skip:
            headers.add(k, v)
    return headers


def _build_cert(cert_path: str, key_path: str, hostnames: list[str], ips: list[str]) -> None:
    """Generate a long-lived self-signed cert with SAN, if not present yet."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san: list[x509.GeneralName] = []
    for host in hostnames:
        try:
            san.append(x509.DNSName(host))
        except Exception:  # noqa: BLE001
            pass
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:  # noqa: BLE001
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BMS Intercom")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    # Private key — readable only by the owner (best-effort; no-op on Windows).
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    _LOGGER.info("BMS Intercom: создан самоподписанный сертификат (%s)", cert_path)


class HTTPSProxy:
    """Self-contained HTTPS reverse proxy to the local Home Assistant."""

    def __init__(self, hass, port: int) -> None:
        self.hass = hass
        self.port = port
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self._backend, self._ws_backend = backend_urls(hass)

    async def async_start(self) -> None:
        cert_path = self.hass.config.path("bms_intercom", "https_cert.pem")
        key_path = self.hass.config.path("bms_intercom", "https_key.pem")
        hostnames, ips = await self._collect_names()
        await self.hass.async_add_executor_job(
            _build_cert, cert_path, key_path, hostnames, ips
        )

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        await self.hass.async_add_executor_job(
            ssl_ctx.load_cert_chain, cert_path, key_path
        )

        self._session = aiohttp.ClientSession(
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None),
        )
        app = web.Application(client_max_size=1024 ** 3)
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port, ssl_context=ssl_ctx)
        try:
            await site.start()
        except OSError as err:
            _LOGGER.error("BMS Intercom: не удалось занять порт %s: %s", self.port, err)
            await self.async_stop()
            return
        _LOGGER.info(
            "BMS Intercom: локальный HTTPS поднят на порту %s → %s", self.port, self._backend
        )

    async def async_stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _collect_names(self) -> tuple[list[str], list[str]]:
        hostnames = ["localhost", "homeassistant.local", "homeassistant"]
        ips = ["127.0.0.1"]
        try:
            from homeassistant.components.network import async_get_enabled_source_ips

            for addr in await async_get_enabled_source_ips(self.hass):
                text = str(addr)
                if not text.startswith(("127.", "::1", "fe80")):
                    ips.append(text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("BMS Intercom: не удалось получить IP хоста: %s", err)
        return hostnames, sorted(set(ips))

    # --- proxying ---------------------------------------------------------
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._ws(request)
        return await self._http(request)

    async def _http(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        url = self._backend + request.rel_url.raw_path_qs
        headers = _forward_headers(request, _HTTP_SKIP)
        try:
            backend = await self._session.request(
                request.method, url, headers=headers,
                data=request.content if request.body_exists else None,
                allow_redirects=False, ssl=_BACKEND_SSL,
            )
        except aiohttp.ClientError as err:
            return web.Response(status=502, text=f"intercom proxy: {err}")
        resp = web.StreamResponse(status=backend.status)
        for k, v in backend.headers.items():
            if k.lower() not in _HOP:
                resp.headers.add(k, v)
        try:
            await resp.prepare(request)
            async for chunk in backend.content.iter_chunked(65536):
                await resp.write(chunk)
            await resp.write_eof()
        except (asyncio.CancelledError, ConnectionResetError, aiohttp.ClientError):
            pass
        finally:
            backend.release()
        return resp

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        raw_proto = request.headers.get("Sec-WebSocket-Protocol", "")
        protocols = tuple(p.strip() for p in raw_proto.split(",") if p.strip())
        server_ws = web.WebSocketResponse(protocols=protocols)
        await server_ws.prepare(request)
        url = self._ws_backend + request.rel_url.raw_path_qs

        # Cookies и прочие заголовки — тем же путём, что и в HTTP (см. _WS_SKIP).
        ws_headers = _forward_headers(request, _WS_SKIP)

        try:
            client_ws = await self._session.ws_connect(
                url, heartbeat=30, headers=ws_headers, protocols=protocols,
                ssl=_BACKEND_SSL,
            )
        except aiohttp.ClientError as err:
            _LOGGER.debug("BMS Intercom: ws backend error: %s", err)
            await server_ws.close()
            return server_ws

        async def pump(src, dst) -> None:
            try:
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                        break
            except (aiohttp.ClientError, ConnectionResetError, RuntimeError):
                # Другая сторона уже закрывается — при разрыве это нормально.
                pass

        tasks = [
            asyncio.create_task(pump(client_ws, server_ws)),
            asyncio.create_task(pump(server_ws, client_ws)),
        ]
        try:
            # As soon as one direction ends, tear the other down — don't wait
            # for the far side to also close (avoids hung connections).
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            # Заберём результаты/исключения отменённых задач, иначе HA
            # залогирует «Task exception was never retrieved».
            await asyncio.gather(*pending, return_exceptions=True)
        except (asyncio.CancelledError, aiohttp.ClientError, ConnectionResetError):
            pass
        finally:
            await client_ws.close()
            await server_ws.close()
        return server_ws
