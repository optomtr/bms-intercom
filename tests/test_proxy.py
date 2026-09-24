"""Встроенный HTTPS-прокси не должен ставить X-Forwarded-For.

Мина из форка (7bc3ebc, 6bbbdad): прокси сам ставил X-Forwarded-For/Proto/Host
на запросы к HA. Home Assistant отвечает 400 на любой запрос с X-Forwarded-For,
если в `http:` не включены use_x_forwarded_for и trusted_proxies — а по
умолчанию они выключены. Итог: порт 8443 не работал на обычной установке.

Здесь настоящий код прокси (HTTP и WebSocket) гоняется против фейкового HA, у
которого стоит НАСТОЯЩИЙ middleware Home Assistant (forwarded.py) с настройками
по умолчанию. Проверяем: присланные клиентом пересылочные заголовки вырезаны,
свои прокси не добавляет, cookies доходят (фикс Ingress из форка цел).

Run: python -m unittest discover -s tests -v   (нужен aiohttp; HA — по желанию)
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

try:
    import homeassistant  # noqa: F401  - раньше voluptuous, как в HA
except ImportError:  # pragma: no cover
    pass

try:
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
except ImportError:  # pragma: no cover
    aiohttp = None

try:
    from homeassistant.components.http.forwarded import async_setup_forwarded
except ImportError:  # pragma: no cover
    async_setup_forwarded = None

from _loader import load

FORWARD = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
           "forwarded", "x-real-ip")
SPOOF = {
    "X-Forwarded-For": "6.6.6.6",
    "X-Forwarded-Proto": "http",
    "X-Forwarded-Host": "evil.example",
    "Forwarded": "for=6.6.6.6",
    "X-Real-IP": "6.6.6.6",
    "Cookie": "ingress_session=abc123",
}


def _forward_seen(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() in FORWARD}


def _fake_ha(seen: dict) -> web.Application:
    """HA на 127.0.0.1: реальный forwarded-middleware с настройками по умолчанию."""
    app = web.Application()
    if async_setup_forwarded is not None:
        async_setup_forwarded(app, None, [])   # как HA без `http:` в конфиге

    async def page(request: web.Request) -> web.Response:
        seen["http"] = dict(request.headers)
        return web.Response(text="ok")

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        seen["ws"] = dict(request.headers)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("hello")
        await ws.close()
        return ws

    app.router.add_get("/api/websocket", websocket)
    app.router.add_get("/{tail:.*}", page)
    return app


class FakeHass:
    """Ровно то, что прокси спрашивает у HA: порт и SSL компонента http."""

    def __init__(self, server_port=None, ssl_certificate=None, api=None):
        self.http = SimpleNamespace(server_port=server_port, ssl_certificate=ssl_certificate)
        self.config = SimpleNamespace(api=api)


@unittest.skipIf(aiohttp is None, "aiohttp not installed")
class TestBackendFromHaPort(unittest.TestCase):
    """0.3.3: адрес бэкенда был зашит 127.0.0.1:8123, а HA на объекте на :80 → 502."""

    def setUp(self):
        self.proxy_mod = load("proxy")

    def urls(self, hass):
        proxy = self.proxy_mod.HTTPSProxy(hass, 8443)
        return proxy._backend, proxy._ws_backend

    def test_port_80_from_hass_http(self):
        self.assertEqual(
            self.urls(FakeHass(server_port=80)),
            ("http://127.0.0.1:80", "ws://127.0.0.1:80"),
        )

    def test_api_config_is_the_fallback(self):
        hass = FakeHass(api=SimpleNamespace(port=8124, use_ssl=False))
        self.assertEqual(self.urls(hass), ("http://127.0.0.1:8124", "ws://127.0.0.1:8124"))

    def test_nothing_known_falls_back_to_8123(self):
        self.assertEqual(self.urls(None), ("http://127.0.0.1:8123", "ws://127.0.0.1:8123"))

    def test_ha_with_own_ssl_is_reached_over_tls(self):
        for hass in (
            FakeHass(server_port=443, ssl_certificate="/ssl/fullchain.pem"),
            FakeHass(api=SimpleNamespace(port=443, use_ssl=True)),
        ):
            with self.subTest(hass=vars(hass.http)):
                self.assertEqual(
                    self.urls(hass), ("https://127.0.0.1:443", "wss://127.0.0.1:443")
                )


@unittest.skipIf(aiohttp is None, "aiohttp not installed")
class TestProxyForwardHeaders(unittest.TestCase):
    def setUp(self):
        self.proxy_mod = load("proxy")

    async def _run(self, action):
        seen: dict = {}
        backend = TestServer(_fake_ha(seen), host="127.0.0.1")
        await backend.start_server()

        # Порт фейкового HA прокси узнаёт так же, как на объекте: от hass.http.
        proxy = self.proxy_mod.HTTPSProxy(FakeHass(server_port=backend.port), 0)
        # Та же сессия, что строит async_start (без TLS — он тут не при чём).
        proxy._session = aiohttp.ClientSession(
            auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar()
        )
        front = web.Application()
        front.router.add_route("*", "/{path:.*}", proxy._handle)
        server = TestServer(front, host="127.0.0.1")
        await server.start_server()
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as client:
                result = await action(client, f"127.0.0.1:{server.port}")
        finally:
            await server.close()
            await proxy.async_stop()
            await backend.close()
        return result, seen

    def test_http_strips_client_headers_and_adds_none(self):
        async def action(client, addr):
            async with client.get(f"http://{addr}/lovelace/0", headers=SPOOF) as resp:
                return resp.status, await resp.text()

        (status, body), seen = asyncio.run(self._run(action))
        # С XFF настоящий middleware HA ответил бы 400.
        self.assertEqual(status, 200, body)
        self.assertIn("http", seen)
        self.assertEqual(_forward_seen(seen["http"]), {})
        self.assertEqual(seen["http"].get("Cookie"), "ingress_session=abc123")

    def test_websocket_strips_client_headers_and_adds_none(self):
        async def action(client, addr):
            async with client.ws_connect(f"http://{addr}/api/websocket", headers=SPOOF) as ws:
                msg = await ws.receive(timeout=5)
                return msg.data

        greeting, seen = asyncio.run(self._run(action))
        self.assertEqual(greeting, "hello")
        self.assertIn("ws", seen, "WebSocket не дошёл до HA (400 на рукопожатии?)")
        self.assertEqual(_forward_seen(seen["ws"]), {})
        # Фикс Ingress из форка: cookies клиента доходят и в WebSocket.
        self.assertEqual(seen["ws"].get("Cookie"), "ingress_session=abc123")


if __name__ == "__main__":
    unittest.main()
