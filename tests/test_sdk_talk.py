"""Голос оператора → DS-K1T341AM через помощник HCNetSDK (sdkaudio/talkroute).

Терминал не принимает звук по ISAPI, но принимает по HCNetSDK. Помощник
`bms_talk` здесь — tests/fake_bms_talk.py с тем же протоколом stdin/stdout:
настоящий процесс, настоящие трубы, настоящие WS-обработчики из __init__.py.

Needs Home Assistant importable (the prepared venv); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import homeassistant  # noqa: F401
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

from _loader import load

if HAVE_HA:
    sdkaudio = load("sdkaudio")
    entity_mod = load("entity")
    integration = load("__init__")
    talkroute = load("talkroute")
    from test_device import REAL, DeviceTestCase
    from test_ds_k1t341am import Panel
    from test_reject import sent_commands
    from test_test_call import CallPanel
else:  # pragma: no cover
    DeviceTestCase = unittest.TestCase

FAKE = Path(__file__).resolve().parent / "fake_bms_talk.py"


class FakeConnection:
    def __init__(self):
        self.results: list[int] = []

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)


class SDKTalkCase(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.record = os.path.join(self.tmp.name, "rec")
        self.use_helper([sys.executable, str(FAKE), self.record])
        self.conn = FakeConnection()

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def use_helper(self, cmd):
        sdkaudio._prepare_helper = lambda: (cmd, self.tmp.name)
        sdkaudio.reset_cache()

    def attrs(self, device):
        return entity_mod.BMSIntercomEntity(device, "k").intercom_attributes

    def recorded(self, suffix):
        path = self.record + suffix
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    async def up(self, data=None, panel=None):
        # DS-K1T341AM: TwoWayAudio → 404 notSupport (у CallPanel — тоже).
        self.use_panel(panel or Panel())
        device, hass, entry = self.make_device(data)
        hass.data["bms_intercom"] = {entry.entry_id: device}
        await device.async_setup()
        await self.settle(entry, lambda: device._sdk_ok is not None
                          and device.signal_supported is not None)
        return device, hass, entry

    async def ws(self, hass, name, **extra):
        msg = {"id": len(self.conn.results) + 1, "type": f"bms_intercom/{name}",
               "entry_id": "entry1", **extra}
        await getattr(integration, f"_ws_{name}").__wrapped__(hass, self.conn, msg)


class TestTalkThroughTheHelper(SDKTalkCase):
    def test_bytes_from_the_browser_reach_the_helper(self):
        chunk = bytes(range(256)) * 4   # ~1 КБ, как кусок микрофона браузера

        async def main():
            device, hass, entry = await self.up()
            attrs = self.attrs(device)
            self.assertEqual((attrs["talk_supported"], attrs["talk_via"]), ("yes", "sdk"))
            self.assertNotIn("talk_hint", attrs)

            await self.ws(hass, "talk_start")
            start = json.loads(self.recorded(".start"))
            self.assertEqual(start, {"host": REAL["host"], "port": 8000, "user": "admin",
                                     "password": REAL["password"], "channel": 1})
            await self.ws(hass, "talk_data", data=base64.b64encode(chunk).decode())
            await self.ws(hass, "talk_data", data=base64.b64encode(chunk).decode())
            await self.settle(entry, lambda: self.recorded(".audio") == chunk * 2)
            self.assertEqual(self.recorded(".audio"), chunk * 2, "звук не дошёл до помощника")

            await self.ws(hass, "talk_stop")
            self.assertIsNotNone(self.recorded(".stopped"), "помощник не понял конец разговора")
            self.assertIsNone(device._talk)
            self.assertEqual(self.conn.results, [1, 2, 3, 4])
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_wrong_password_is_a_clear_error_and_never_logged(self):
        secret = "wrong-Pa55!"

        async def main():
            device, hass, entry = await self.up({**REAL, "password": secret})
            with self.assertLogs("bms_intercom_under_test", level="DEBUG") as cm:
                await self.ws(hass, "talk_start")
            log = "\n".join(cm.output)
            self.assertIn("неверный логин или пароль терминала (код 1)", device.talk_error)
            self.assertEqual(self.attrs(device)["talk_error"], device.talk_error)
            self.assertIn("неверный логин или пароль", log)
            # Пароль в журнал не попал, хотя помощник сам написал его в stderr…
            self.assertNotIn(secret, log, "пароль терминала в журнале HA")
            # …а сама строка журнала помощника дошла (иначе проверка пустая).
            self.assertIn("login admin:***@", log)
            self.assertIsNone(device._talk)
            # Повторный вход с плохим паролем заблокировал бы учётку терминала.
            self.assertEqual(self.recorded(".runs"), b"1", "неверный пароль повторён")
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestBusyTerminal(SDKTalkCase):
    """0.3.6: терминал в своём режиме вызова отвечает на StartVoiceCom кодом 11.

    С живого DS-K1T341AM: вход проходит, голос — «ошибка 11», пока на
    терминале настоящий вызов. Лечение — reject/hangUp на терминал без смены
    вызова в HA и повтор открытия.
    """

    def setUp(self):
        super().setUp()
        self._pauses = talkroute.SDK_BUSY_PAUSES
        talkroute.SDK_BUSY_PAUSES = (0.01,) * len(self._pauses)

    def tearDown(self):
        talkroute.SDK_BUSY_PAUSES = self._pauses
        super().tearDown()

    async def answered(self, busy):
        """Настоящий вызов принят в поп-апе; помощник занят первые `busy` раз."""
        self.use_helper([sys.executable, str(FAKE), self.record, "--busy", str(busy)])
        panel = CallPanel("rings")
        device, hass, entry = await self.up(panel=panel)
        self.assertIs(device.signal_supported, True)
        device._apply_panel_state("ringing")
        await device.async_answer()
        self.assertEqual(device.call_state, "answered")
        panel.signals.clear()
        return device, hass, entry, panel

    def runs(self):
        return int(self.recorded(".runs") or b"0")

    def test_busy_once_frees_the_terminal_keeps_the_call_and_talks(self):
        chunk = bytes(range(256)) * 3

        async def main():
            device, hass, entry, panel = await self.answered(busy=1)
            with self.assertLogs("bms_intercom_under_test", level="INFO") as cm:
                await self.ws(hass, "talk_start")
            self.assertEqual(sent_commands(panel), ["reject", "hangUp"])
            self.assertEqual(self.runs(), 2, "второго открытия не было")
            self.assertIn("открыт с попытки 2 из 5", "\n".join(cm.output))
            self.assertIsNotNone(device._talk)
            self.assertEqual(device.talk_error, "")
            # Вызов в HA не тронут: разговор, латч, без паузы «после Сбросить».
            self.assertEqual(device.call_state, "answered")
            self.assertTrue(device._answered)
            self.assertLess(device._ring_quiet_until, time.monotonic())
            device._apply_panel_state("idle")   # терминал освобождён — опрос говорит idle
            self.assertEqual(device.call_state, "answered", "поп-ап закрылся")

            await self.ws(hass, "talk_data", data=base64.b64encode(chunk).decode())
            await self.settle(entry, lambda: self.recorded(".audio") == chunk)
            self.assertEqual(self.recorded(".audio"), chunk, "голос не пошёл")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_always_busy_is_a_clear_error_and_the_popup_stays(self):
        async def main():
            device, hass, entry, panel = await self.answered(busy=99)
            await self.ws(hass, "talk_start")
            self.assertEqual(self.runs(), 5)
            self.assertEqual(sent_commands(panel), ["reject", "hangUp"] * 4)
            self.assertEqual(
                device.talk_error,
                "голос по SDK: терминал не открыл голос — занят своим вызовом "
                "(код 11); не освободился за 5 попыток",
            )
            self.assertEqual(self.attrs(device)["talk_error"], device.talk_error)
            self.assertIsNone(device._talk)
            self.assertEqual(device.call_state, "answered")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_double_talk_start_runs_one_helper(self):
        async def main():
            device, hass, entry, panel = await self.answered(busy=0)
            # «Ответить» и кнопка микрофона почти одновременно.
            await asyncio.gather(self.ws(hass, "talk_start"), self.ws(hass, "talk_start"))
            self.assertEqual(self.runs(), 1, "запущен второй помощник")
            await self.ws(hass, "talk_start")   # голос уже открыт — берём его
            self.assertEqual(self.runs(), 1)
            self.assertIsNotNone(device._talk)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_talk_stop_during_retries_stops_them(self):
        async def main():
            talkroute.SDK_BUSY_PAUSES = (0.3,) * len(self._pauses)
            device, hass, entry, panel = await self.answered(busy=99)
            start = asyncio.create_task(self.ws(hass, "talk_start"))
            await self.settle(entry, lambda: len(panel.signals) >= 2)
            await self.ws(hass, "talk_stop")        # «Сбросить» во время повторов
            await asyncio.wait_for(start, 3)
            runs = self.runs()
            await asyncio.sleep(0.5)
            self.assertEqual(self.runs(), runs, "повторы идут после talk_stop")
            self.assertLess(runs, 5)
            self.assertIsNone(device._talk)
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestNoHelper(SDKTalkCase):
    def test_no_helper_for_this_platform_says_why(self):
        async def main():
            sdkaudio._prepare_helper = lambda: sdkaudio.NO_PLATFORM
            device, hass, entry = await self.up()
            attrs = self.attrs(device)
            self.assertEqual(
                (attrs["talk_supported"], attrs["talk_via"], attrs["talk_hint"]),
                ("no", "none", "голос по SDK недоступен на этой платформе"),
            )
            await self.ws(hass, "talk_start")
            self.assertIsNone(self.recorded(".start"))
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_failed_selftest_is_rechecked_on_talk_start(self):
        async def main():
            self.use_helper([sys.executable, "-c", "import sys; sys.exit(3)"])
            device, hass, entry = await self.up()
            self.assertEqual(self.attrs(device)["talk_hint"],
                             "помощник голоса не прошёл самопроверку (код выхода 3)")
            # Помощник ожил (сбой был разовым), срок повторной проверки вышел.
            sdkaudio._prepare_helper = lambda: (
                [sys.executable, str(FAKE), self.record], self.tmp.name)
            sdkaudio._CHECK["until"] = 0
            await self.ws(hass, "talk_start")
            self.assertEqual(self.attrs(device)["talk_via"], "sdk")
            self.assertIsNotNone(self.recorded(".start"), "talk_start не пошёл в SDK")
            await self.shutdown(device, entry)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
