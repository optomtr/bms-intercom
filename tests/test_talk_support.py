"""Микрофон оператора → панель: модель без two-way audio и sessionId.

С объекта (DS-K1T341AM V3.2.30): GET /ISAPI/System/TwoWayAudio/channels →
404 notSupport. До 0.3.3 talkback всё равно шёл в open/audioData («audioData
HTTP 400»), а поп-ап показывал «Микрофон» рабочим. Теперь панель при изучении
профиля получает talk_supported=False → атрибут `talk_supported: no`.

Новые прошивки отвечают на PUT …/open телом с <sessionId>, и audioData без
?sessionId= даёт 400 — проверяем на настоящем сыром сокете.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import unittest

try:
    import homeassistant  # noqa: F401
    import httpx
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

from _loader import load

if HAVE_HA:
    isapi = load("isapi")
    talkback = load("talkback")
    entity_mod = load("entity")
    from test_device import DeviceTestCase
    from test_ds_k1t341am import NOT_FOUND, Panel, make_client
else:  # pragma: no cover
    DeviceTestCase = unittest.TestCase

CHANNELS = (
    "<TwoWayAudioChannelList><TwoWayAudioChannel><id>1</id>"
    "<audioCompressionType>G.711ulaw</audioCompressionType>"
    "</TwoWayAudioChannel></TwoWayAudioChannelList>"
)


class PanelWithTalk:
    """Модель, у которой two-way audio есть; open может вернуть sessionId."""

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.calls: list[str] = []

    def __call__(self, request):
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/ISAPI/System/TwoWayAudio/channels":
            return httpx.Response(200, text=CHANNELS)
        if path.endswith("/open") and self.session_id is not None:
            return httpx.Response(
                200, text="<TwoWayAudioSession><sessionId>%s</sessionId>"
                          "</TwoWayAudioSession>" % self.session_id)
        return httpx.Response(200, text="<ResponseStatus><statusCode>1</statusCode>"
                                        "</ResponseStatus>")


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class TestProfileLearnsTalkSupport(unittest.TestCase):
    def test_404_not_support_means_no_talk(self):
        panel = Panel()
        client = make_client(panel)
        asyncio.run(client.async_ensure_twoway_codec())   # не бросает
        self.assertIs(client.talk_supported, False)
        self.assertNotIn("PUT", " ".join(panel.paths))

    def test_channels_list_means_talk(self):
        client = make_client(PanelWithTalk())
        asyncio.run(client.async_ensure_twoway_codec())
        self.assertIs(client.talk_supported, True)

    def test_offline_panel_gives_no_verdict(self):
        def offline(request):
            raise httpx.ConnectError("нет связи")

        client = make_client(offline)
        with self.assertRaises(isapi.ISAPIError):
            asyncio.run(client.async_ensure_twoway_codec())
        self.assertIsNone(client.talk_supported)


class TestAttributeOnTheEntities(DeviceTestCase):
    def attrs(self, device):
        return entity_mod.BMSIntercomEntity(device, "k").intercom_attributes

    def test_ds_k1t341am_says_talk_supported_no(self):
        self.use_panel(Panel())

        async def main():
            device, _hass, entry = self.make_device()
            self.assertEqual(self.attrs(device)["talk_supported"], "unknown")
            await device.async_setup()
            await self.settle(entry, lambda: device.talk_supported is not None)
            self.assertIs(device.talk_supported, False)
            self.assertEqual(self.attrs(device)["talk_supported"], "no")
            # talk_start к такой панели не идёт вовсе (ни open, ни audioData).
            opened = []
            real = talkback.TwoWayAudioSession.async_open

            async def spy(sess):
                opened.append(sess)
                await real(sess)

            talkback.TwoWayAudioSession.async_open = spy
            try:
                await device.async_talk_start()
            finally:
                talkback.TwoWayAudioSession.async_open = real
            self.assertEqual(opened, [])
            await self.shutdown(device, entry)

        asyncio.run(main())


class AudioSocket:
    """Настоящий TCP-сервер на месте панели: пишет строку запроса audioData."""

    def __init__(self):
        self.request_lines: list[str] = []

    async def handle(self, reader, writer):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(1024)
            if not chunk:
                break
            head += chunk
        self.request_lines.append(head.split(b"\r\n", 1)[0].decode())
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()
        await reader.read()   # до закрытия сокета сессией
        writer.close()


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class TestTwoWayAudioSession(unittest.TestCase):
    async def open_session(self, handler):
        audio = AudioSocket()
        server = await asyncio.start_server(audio.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sess = talkback.TwoWayAudioSession("127.0.0.1", port, "admin", "pw")
        await sess._client.aclose()
        sess._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await sess.async_open()
            return None, audio
        except talkback.TwoWayAudioError as err:
            return err, audio
        finally:
            await sess.async_close()
            server.close()
            await server.wait_closed()

    def test_not_support_is_a_clear_error_without_audio_data(self):
        calls = []

        def handler(request):
            calls.append(f"{request.method} {request.url.path}")
            return httpx.Response(404, text=NOT_FOUND)

        err, audio = asyncio.run(self.open_session(handler))
        self.assertIsInstance(err, talkback.TwoWayAudioUnsupported)
        self.assertIn("не поддерживает двусторонний звук", str(err))
        self.assertEqual(audio.request_lines, [], "audioData всё же открывали")
        self.assertNotIn("PUT /ISAPI/System/TwoWayAudio/channels/1/open", calls)

    def test_session_id_from_open_goes_into_audio_data(self):
        err, audio = asyncio.run(self.open_session(PanelWithTalk("a1b2c3")))
        self.assertIsNone(err)
        self.assertEqual(audio.request_lines, [
            "PUT /ISAPI/System/TwoWayAudio/channels/1/audioData?sessionId=a1b2c3 HTTP/1.1"
        ])

    def test_old_firmware_without_session_id_keeps_the_bare_path(self):
        err, audio = asyncio.run(self.open_session(PanelWithTalk(None)))
        self.assertIsNone(err)
        self.assertEqual(audio.request_lines, [
            "PUT /ISAPI/System/TwoWayAudio/channels/1/audioData HTTP/1.1"
        ])


if __name__ == "__main__":
    unittest.main()
