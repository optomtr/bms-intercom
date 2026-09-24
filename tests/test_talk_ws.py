"""WS-команда bms_intercom/talk_data: до панели доходит только годный звук.

Браузер шлёт микрофон кусками base64 (~1 КБ). Мусорный base64 и кусок больше
MAX_TALK_B64 (64 КБ) не должны дойти до панели, а ответ на команду всё равно
уходит — иначе фронтенд копил бы висящие промисы.

Гоняется НАСТОЯЩИЙ обработчик из __init__.py (через __wrapped__ декоратора
async_response), с фейковыми hass/соединением/устройством.

Needs Home Assistant importable (the prepared venv); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import base64
import unittest

try:
    import homeassistant  # noqa: F401
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

from _loader import load

if HAVE_HA:
    integration = load("__init__")
    talkback = load("talkback")


class FakeDevice:
    def __init__(self):
        self.sent: list[bytes] = []

    async def async_talk_send(self, data: bytes) -> None:
        self.sent.append(data)


class FakeConnection:
    def __init__(self):
        self.results: list[int] = []

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)


class FakeHass:
    def __init__(self, device):
        self.data = {"bms_intercom": {"e1": device}}


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class TestTalkData(unittest.TestCase):
    def send(self, data):
        device = FakeDevice()
        conn = FakeConnection()
        msg = {"id": 7, "type": "bms_intercom/talk_data", "entry_id": "e1", "data": data}
        handler = integration._ws_talk_data.__wrapped__
        asyncio.run(handler(FakeHass(device), conn, msg))
        self.assertEqual(conn.results, [7], "ответ на команду не ушёл")
        return device.sent

    def test_valid_chunk_reaches_the_panel(self):
        chunk = bytes(range(256)) * 4
        self.assertEqual(self.send(base64.b64encode(chunk).decode()), [chunk])

    def test_garbage_base64_does_not_reach_the_panel(self):
        for junk in ("!!!не-base64!!!", "QUJD$$RA==", "QUJ", "   "):
            with self.subTest(junk=junk):
                self.assertEqual(self.send(junk), [])

    def test_chunk_over_64kb_does_not_reach_the_panel(self):
        big = base64.b64encode(b"\x7f" * 49152 + b"\x00\x00\x00").decode()
        self.assertGreater(len(big), talkback.MAX_TALK_B64)
        self.assertEqual(self.send(big), [])

    def test_chunk_at_the_limit_still_passes(self):
        edge = base64.b64encode(b"\x55" * 49152).decode()
        self.assertEqual(len(edge), talkback.MAX_TALK_B64)
        self.assertEqual(len(self.send(edge)[0]), 49152)

    def test_unknown_intercom_is_ignored_but_answered(self):
        device = FakeDevice()
        conn = FakeConnection()
        msg = {"id": 9, "type": "bms_intercom/talk_data", "entry_id": "nope",
               "data": base64.b64encode(b"abc").decode()}
        asyncio.run(integration._ws_talk_data.__wrapped__(FakeHass(device), conn, msg))
        self.assertEqual((device.sent, conn.results), ([], [9]))


if __name__ == "__main__":
    unittest.main()
