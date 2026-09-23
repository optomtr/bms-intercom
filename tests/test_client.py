"""ISAPIClient against a fake panel (httpx MockTransport).

Proves the behaviour the live site needs: digest first, one retry with basic,
remember what worked, walk the candidate list until something answers.

Run: python3 -m unittest discover -s tests -v   (skipped when httpx is absent)
"""
from __future__ import annotations

import asyncio
import unittest

try:
    import httpx
except ImportError:  # pragma: no cover - plain interpreter without httpx
    httpx = None

from _loader import load

if httpx is not None:
    isapi = load("isapi")


def run(coro):
    return asyncio.run(coro)


def make_client(handler, **kwargs):
    client = isapi.ISAPIClient("192.168.70.121", "admin", "Sekret123!", **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@unittest.skipIf(httpx is None, "httpx not installed")
class TestAuth(unittest.TestCase):
    def test_digest_is_used_when_the_panel_asks_for_digest(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            seen.append(auth.split(" ")[0] if auth else "none")
            if auth.lower().startswith("digest "):
                return httpx.Response(200, text="<DeviceInfo/>")
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest qop="auth", realm="DS-K1T341AM", '
                    'nonce="abc", opaque="xyz"'
                },
            )

        client = make_client(handler)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "digest")
        self.assertIn("Digest", seen)
        run(client.async_close())

    def test_basic_only_panel_falls_back_once_and_is_remembered(self):
        schemes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            schemes.append(auth.split(" ")[0] if auth else "none")
            if auth.lower().startswith("basic "):
                return httpx.Response(200, text="<DeviceInfo/>")
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Basic realm="DS-K1T341AM"'}
            )

        client = make_client(handler)
        run(client.async_verify())
        self.assertEqual(client.auth_mode, "basic")
        # Remembered: the next call goes out as basic straight away.
        schemes.clear()
        run(client.async_get_call_status())
        self.assertNotIn("none", schemes)
        run(client.async_close())

    def test_wrong_credentials_raise_auth_error(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="y"'}
            )

        client = make_client(handler)
        with self.assertRaises(isapi.ISAPIAuthError):
            run(client.async_verify())
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestEndpointFallback(unittest.TestCase):
    def test_door_falls_through_to_the_json_shape(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
            paths.append(path)
            if "format=json" in path:
                return httpx.Response(200, text='{"statusCode":1}')
            return httpx.Response(404, text="<ResponseStatus/>")

        client = make_client(handler, door_no=1)
        run(client.async_open_door())
        self.assertEqual(paths[0], "/ISAPI/AccessControl/RemoteControl/door/1")
        self.assertTrue(paths[1].endswith("?format=json"))
        # Remembered: no second walk through the candidate list.
        paths.clear()
        run(client.async_open_door())
        self.assertEqual(len(paths), 1)
        run(client.async_close())

    def test_missing_answer_endpoint_is_tolerated(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(501, text="not implemented")

        client = make_client(handler)
        self.assertFalse(run(client.async_answer()))  # no raise: entity survives
        run(client.async_close())

    def test_rtsp_path_follows_the_channel_the_panel_confirms(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ISAPI/Streaming/channels/101":
                return httpx.Response(404)
            return httpx.Response(200, text="<StreamingChannel/>")

        client = make_client(handler, rtsp_port=554)
        self.assertEqual(run(client.async_select_channel()), "/Streaming/Channels/102")
        self.assertIn("/Streaming/Channels/102", client.rtsp_url())
        self.assertNotIn("Sekret123", client.rtsp_url(redacted=True))
        run(client.async_close())

    def test_snapshot_uses_the_isapi_picture_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ISAPI/Streaming/channels/101/picture":
                return httpx.Response(
                    200, content=b"\xff\xd8\xff\xe0jpeg",
                    headers={"Content-Type": "image/jpeg"},
                )
            return httpx.Response(404)

        client = make_client(handler)
        self.assertEqual(run(client.async_snapshot()), b"\xff\xd8\xff\xe0jpeg")
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestProbe(unittest.TestCase):
    def _client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/ISAPI/System/deviceInfo":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/xml"},
                    text="<DeviceInfo><model>DS-K1T341AM</model>"
                    "<firmwareVersion>V3.2.30</firmwareVersion></DeviceInfo>",
                )
            if path == "/ISAPI/Streaming/channels/101/picture":
                return httpx.Response(
                    200, content=b"\xff\xd8" * 20,
                    headers={"Content-Type": "image/jpeg"},
                )
            return httpx.Response(404, text="<ResponseStatus><statusString>Invalid "
                                            "Operation</statusString></ResponseStatus>")

        return make_client(handler)

    def test_probe_reports_status_auth_and_model_without_secrets(self):
        client = self._client()
        results, summary = run(client.async_probe())
        report = load("probe").format_probe_report(
            results, host="192.168.70.121", summary=summary
        )
        self.assertIn("DS-K1T341AM", report)
        self.assertIn("auth=digest", report)
        self.assertIn("-> 404", report)
        self.assertNotIn("Sekret123", report)
        self.assertIn("не трогали", summary["реле двери"])  # door stays shut
        self.assertIn("двоичных данных", report)  # jpeg body is not dumped
        run(client.async_close())

    def test_probe_does_not_open_the_door_by_default(self):
        opened: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "RemoteControl/door" in request.url.path and request.method == "PUT":
                opened.append(request.url.path)
            return httpx.Response(200, text="<x/>")

        client = make_client(handler)
        run(client.async_probe())
        self.assertEqual(opened, [])
        run(client.async_probe(test_door=True))
        self.assertEqual(len(opened), 1)
        run(client.async_close())


if __name__ == "__main__":
    unittest.main()
