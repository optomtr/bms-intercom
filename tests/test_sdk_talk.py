"""Голос оператора → DS-K1T341AM через постоянный помощник HCNetSDK (0.3.8).

Терминал не принимает звук по ISAPI, но принимает по HCNetSDK. Помощник
`bms_talk --serve` здесь — tests/fake_bms_talk.py с тем же протоколом:
настоящий процесс, настоящие трубы, настоящие WS-обработчики из __init__.py.

С объекта (HA Green): однократный помощник 0.3.7 тратил ~5,5 с на Init+вход
при каждом «Ответить» и столько же на повтор после кода 11 — голос через
~12 с. Теперь вход выполняется при загрузке, а «Ответить» — это reject/hangUp
и команда S тому же процессу.

Needs Home Assistant importable (the prepared venv); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
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
    sdkhelper = load("sdkhelper")
    entity_mod = load("entity")
    integration = load("__init__")
    talkroute = load("talkroute")
    from test_device import REAL, DeviceTestCase
    from test_ds_k1t341am import Panel
    from test_reject import sent_commands
    from test_test_call import CallPanel
else:  # pragma: no cover
    DeviceTestCase = unittest.TestCase
    CallPanel = object

FAKE = Path(__file__).resolve().parent / "fake_bms_talk.py"
S_ONE = 'S {"channel": 1}'


class FakeConnection:
    def __init__(self):
        self.results: list[int] = []

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)


class LoggedCallPanel(CallPanel):
    """Панель с callSignal, которая пишет свои команды в журнал фейкового помощника."""

    def __init__(self, mode, events_path):
        super().__init__(mode)
        self.events_path = events_path

    def __call__(self, request):
        response = super().__call__(request)
        if request.url.path == "/ISAPI/VideoIntercom/callSignal" and request.method == "PUT":
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(f"isapi {sent_commands(self)[-1]}\n")
        return response


class SDKTalkCase(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.record = os.path.join(self.tmp.name, "rec")
        self.use_helper()
        self.conn = FakeConnection()
        # Перезапуск упавшего помощника — без секундных пауз.
        for mod, name, value in ((sdkhelper, "RESTART_MIN", 0.05), (sdkhelper, "RESTART_MAX", 0.2)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def patch(self, mod, **values):
        for name, value in values.items():
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    def use_helper(self, *flags):
        cmd = [sys.executable, str(FAKE), self.record, *flags]
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

    def events(self) -> list[str]:
        return (self.recorded(".events") or b"").decode().splitlines()

    def runs(self) -> int:
        return int(self.recorded(".runs") or b"0")

    async def up(self, data=None, panel=None, wait_helper=True):
        # DS-K1T341AM: TwoWayAudio → 404 notSupport (у CallPanel — тоже).
        self.use_panel(panel or Panel())
        device, hass, entry = self.make_device(data)
        hass.data["bms_intercom"] = {entry.entry_id: device}
        await device.async_setup()
        await self.settle(entry, lambda: device._sdk_ok is not None
                          and device.signal_supported is not None)
        if wait_helper:
            await self.settle(entry, lambda: device._sdk_helper is not None and (
                device._sdk_helper.ready or device._sdk_helper.fatal))
        return device, hass, entry

    async def ws(self, hass, name, **extra):
        msg = {"id": len(self.conn.results) + 1, "type": f"bms_intercom/{name}",
               "entry_id": "entry1", **extra}
        await getattr(integration, f"_ws_{name}").__wrapped__(hass, self.conn, msg)

    async def answered(self, *flags):
        """Настоящий вызов принят в поп-апе; помощник уже вошёл заранее."""
        self.use_helper(*flags)
        panel = LoggedCallPanel("rings", self.record + ".events")
        device, hass, entry = await self.up(panel=panel)
        self.assertIs(device.signal_supported, True)
        self.assertTrue(device._sdk_helper.ready, "помощник не вошёл до звонка")
        device._apply_panel_state("ringing")
        await device.async_answer()
        self.assertEqual(device.call_state, "answered")
        panel.signals.clear()
        return device, hass, entry, panel


class TestTalkThroughTheHelper(SDKTalkCase):
    def test_bytes_from_the_browser_reach_the_helper(self):
        chunk = bytes(range(256)) * 4   # ~1 КБ, как кусок микрофона браузера

        async def main():
            device, hass, entry = await self.up()
            attrs = self.attrs(device)
            self.assertEqual((attrs["talk_supported"], attrs["talk_via"]), ("yes", "sdk"))
            self.assertNotIn("talk_hint", attrs)
            # Вход выполнен при загрузке — до всякого «Ответить».
            self.assertEqual(self.events(), ["login"])
            start = json.loads(self.recorded(".start"))
            self.assertEqual(start, {"host": REAL["host"], "port": 8000, "user": "admin",
                                     "password": REAL["password"]})

            await self.ws(hass, "talk_start")
            self.assertEqual(self.events(), ["login", S_ONE])
            await self.ws(hass, "talk_data", data=base64.b64encode(chunk).decode())
            await self.ws(hass, "talk_data", data=base64.b64encode(chunk).decode())
            await self.settle(entry, lambda: self.recorded(".audio") == chunk * 2)
            self.assertEqual(self.recorded(".audio"), chunk * 2, "звук не дошёл до помощника")

            await self.ws(hass, "talk_stop")
            self.assertEqual(self.events()[-1], "E", "помощник не понял конец разговора")
            self.assertIsNone(device._talk)
            self.assertEqual(self.conn.results, [1, 2, 3, 4])
            # Следующий разговор — тем же процессом, без нового входа.
            await self.ws(hass, "talk_start")
            self.assertEqual(self.events(), ["login", S_ONE, "E", S_ONE])
            self.assertEqual(self.runs(), 1)

            await self.shutdown(device, entry)
            self.assertEqual(self.events()[-2:], ["E", "Q"], "выгрузка не закрыла помощника")

        asyncio.run(main())

    def test_wrong_password_is_a_clear_error_and_never_retried(self):
        secret = "wrong-Pa55!"

        async def main():
            with self.assertLogs("bms_intercom_under_test", level="DEBUG") as cm:
                device, hass, entry = await self.up({**REAL, "password": secret})
                # Сказано сразу после загрузки, до всякого звонка.
                self.assertIn("неверный логин или пароль терминала (код 1)", device.talk_error)
                self.assertEqual(self.attrs(device)["talk_error"], device.talk_error)
                device._apply_panel_state("ringing")     # пинок звонка
                await self.ws(hass, "talk_start")
                await self.ws(hass, "talk_start")
                await asyncio.sleep(0.3)                 # пауза перезапуска прошла бы
            log = "\n".join(cm.output)
            self.assertIn("неверный логин или пароль", log)
            self.assertIn("исправьте пароль", log)
            # Пароль в журнал не попал, хотя помощник сам написал его в stderr…
            self.assertNotIn(secret, log, "пароль терминала в журнале HA")
            # …а сама строка журнала помощника дошла (иначе проверка пустая).
            self.assertIn("login admin:***@", log)
            self.assertIsNone(device._talk)
            # Повторный вход с плохим паролем заблокировал бы учётку терминала.
            self.assertEqual(self.runs(), 1, "неверный пароль повторён")
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestAnswer(SDKTalkCase):
    """«Ответить» при настоящем вызове: терминал сначала занят своим вызовом (код 11)."""

    def test_voice_opens_under_a_second_even_if_the_first_reply_is_busy(self):
        chunk = bytes(range(256)) * 3

        async def main():
            device, hass, entry, panel = await self.answered("--busy", "1")
            t0 = time.monotonic()
            with self.assertLogs("bms_intercom_under_test", level="INFO") as cm:
                await self.ws(hass, "talk_start")
            took = time.monotonic() - t0
            self.assertLess(took, 1.0, f"голос открылся за {took:.2f} с")
            self.assertIsNotNone(device._talk)
            self.assertEqual(device.talk_error, "")
            self.assertIn("Голос к терминалу открыт за", "\n".join(cm.output))
            # СНАЧАЛА reject → hangUp, ПОТОМ первый S (иначе первый S гарантированно 11).
            after_answer = self.events()[self.events().index("isapi answer") + 1:]
            self.assertEqual(after_answer, ["isapi reject", "isapi hangUp", S_ONE, S_ONE])
            # Повтор — тем же процессом: нового помощника (и 5,5 с входа) нет.
            self.assertEqual(self.runs(), 1, "на повтор запущен новый помощник")
            self.assertEqual(self.events().count("login"), 1)
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

    def test_always_busy_gives_up_after_the_window_on_the_same_helper(self):
        async def main():
            self.patch(talkroute, SDK_BUSY_WINDOW=0.5, SDK_BUSY_RETRY=0.05, SDK_REFREE_EVERY=0.2)
            device, hass, entry, panel = await self.answered("--busy", "99")
            await self.ws(hass, "talk_start")
            self.assertEqual(
                device.talk_error,
                "голос по SDK: терминал не открыл голос — занят своим вызовом "
                "(код 11); не освободился за 0.5 с",
            )
            self.assertEqual(self.attrs(device)["talk_error"], device.talk_error)
            self.assertGreater(self.events().count(S_ONE), 3)
            self.assertEqual(self.runs(), 1)
            # answer мог дойти после нашего reject — на повторах освобождаем заново.
            self.assertGreaterEqual(sent_commands(panel).count("reject"), 2)
            self.assertIsNone(device._talk)
            self.assertEqual(device.call_state, "answered")
            self.assertTrue(device._sdk_helper.ready, "помощник потерян после отказа")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_double_talk_start_sends_one_start(self):
        async def main():
            device, hass, entry, panel = await self.answered()
            # «Ответить» и кнопка микрофона почти одновременно.
            await asyncio.gather(self.ws(hass, "talk_start"), self.ws(hass, "talk_start"))
            self.assertEqual(self.events().count(S_ONE), 1, "второй S")
            await self.ws(hass, "talk_start")   # голос уже открыт — берём его
            self.assertEqual(self.events().count(S_ONE), 1)
            self.assertEqual(self.runs(), 1)
            self.assertIsNotNone(device._talk)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_talk_stop_during_retries_stops_them_and_closes_voice(self):
        async def main():
            self.patch(talkroute, SDK_BUSY_WINDOW=3.0, SDK_BUSY_RETRY=0.1)
            device, hass, entry, panel = await self.answered("--busy", "99")
            start = asyncio.create_task(self.ws(hass, "talk_start"))
            await self.settle(entry, lambda: self.events().count(S_ONE) >= 2)
            await self.ws(hass, "talk_stop")        # «Сбросить» во время повторов
            await asyncio.wait_for(start, 3)
            tries = self.events().count(S_ONE)
            await asyncio.sleep(0.4)
            self.assertEqual(self.events().count(S_ONE), tries, "повторы идут после talk_stop")
            self.assertEqual(self.events()[-1], "E", "голос у помощника не закрыт")
            self.assertIsNone(device._talk)
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestHelperLife(SDKTalkCase):
    def test_crashed_helper_is_restarted_and_talks_again(self):
        async def main():
            device, hass, entry = await self.up()
            await self.ws(hass, "talk_start")
            self.assertIsNotNone(device._talk)
            os.kill(int(self.recorded(".pid")), signal.SIGKILL)   # помощник упал в разговоре
            await self.settle(entry, lambda: not device._sdk_helper.ready)
            await self.ws(hass, "talk_data", data=base64.b64encode(b"\xff" * 160).decode())
            self.assertIsNone(device._talk, "разговор на мёртвом помощнике не закрыт")
            await self.settle(entry, lambda: device._sdk_helper.ready and self.runs() == 2)
            self.assertEqual(self.runs(), 2, "помощник не перезапущен")
            await self.ws(hass, "talk_start")
            self.assertIsNotNone(device._talk)
            self.assertEqual(self.events()[-2:], ["login", S_ONE])
            self.assertEqual(device.talk_error, "")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_ringing_restarts_a_crashed_helper_without_waiting(self):
        async def main():
            self.patch(sdkhelper, RESTART_MIN=30.0, RESTART_MAX=30.0)
            device, hass, entry = await self.up(panel=CallPanel("rings"))
            os.kill(int(self.recorded(".pid")), signal.SIGKILL)
            await self.settle(entry, lambda: not device._sdk_helper.ready)
            await asyncio.sleep(0.2)
            self.assertEqual(self.runs(), 1)         # ждёт 30 с перед перезапуском…
            device._apply_panel_state("ringing")     # …но звонок не ждёт
            await self.settle(entry, lambda: device._sdk_helper.ready)
            self.assertEqual(self.runs(), 2, "звонок не поднял помощника")
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_talk_start_waits_for_a_helper_still_logging_in(self):
        async def main():
            self.use_helper("--ready-delay", "0.6")
            device, hass, entry = await self.up(wait_helper=False)
            self.assertFalse(device._sdk_helper.ready)
            await self.ws(hass, "talk_start")
            self.assertIsNotNone(device._talk, "не дождались входа помощника")
            self.assertEqual(self.events(), ["login", S_ONE])
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_helper_not_ready_in_time_is_a_clear_error(self):
        async def main():
            self.patch(talkroute, SDK_READY_WAIT=0.2)
            self.use_helper("--ready-delay", "1")
            device, hass, entry = await self.up(wait_helper=False)
            await self.ws(hass, "talk_start")
            self.assertIsNone(device._talk)
            self.assertEqual(device.talk_error,
                             "голос по SDK: помощник не вошёл на терминал за 0.2 с")
            self.assertNotIn(S_ONE, self.events())
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_voice_lost_closes_the_talk_but_keeps_the_login(self):
        async def main():
            self.use_helper("--voice-lost-after", "100")
            device, hass, entry = await self.up()
            await self.ws(hass, "talk_start")
            await self.ws(hass, "talk_data", data=base64.b64encode(b"\xff" * 160).decode())
            await self.settle(entry, lambda: not device._sdk_helper.voice_open)
            await self.ws(hass, "talk_data", data=base64.b64encode(b"\xff" * 160).decode())
            self.assertIsNone(device._talk, "оборванный голос не закрыт")
            self.assertTrue(device._sdk_helper.ready)
            await self.ws(hass, "talk_start")        # новый разговор — без нового входа
            self.assertIsNotNone(device._talk)
            self.assertEqual(self.runs(), 1)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_hung_helper_is_replaced(self):
        async def main():
            self.patch(sdkhelper, COMMAND_TIMEOUT=0.3)
            self.use_helper("--hang")                 # на пинг молчит, как зависший SDK
            device, hass, entry = await self.up()
            with self.assertRaises(sdkaudio.SDKAudioError):
                await device._sdk_helper.async_ping()
            await self.settle(entry, lambda: device._sdk_helper.ready and self.runs() == 2)
            self.assertEqual(self.runs(), 2, "зависший помощник не заменён")
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestNoHelper(SDKTalkCase):
    def test_no_helper_for_this_platform_says_why(self):
        async def main():
            sdkaudio._prepare_helper = lambda: sdkaudio.NO_PLATFORM
            device, hass, entry = await self.up(wait_helper=False)
            attrs = self.attrs(device)
            self.assertEqual(
                (attrs["talk_supported"], attrs["talk_via"], attrs["talk_hint"]),
                ("no", "none", "голос по SDK недоступен на этой платформе"),
            )
            self.assertIsNone(device._sdk_helper)
            await self.ws(hass, "talk_start")
            self.assertIsNone(self.recorded(".start"))
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_failed_selftest_is_rechecked_on_talk_start(self):
        async def main():
            self.use_helper()
            sdkaudio._prepare_helper = lambda: ([sys.executable, "-c", "import sys; sys.exit(3)"],
                                                self.tmp.name)
            device, hass, entry = await self.up(wait_helper=False)
            self.assertEqual(self.attrs(device)["talk_hint"],
                             "помощник голоса не прошёл самопроверку (код выхода 3)")
            # Помощник ожил (сбой был разовым), срок повторной проверки вышел.
            sdkaudio._prepare_helper = lambda: (
                [sys.executable, str(FAKE), self.record], self.tmp.name)
            sdkaudio._CHECK["until"] = 0
            await self.ws(hass, "talk_start")
            self.assertEqual(self.attrs(device)["talk_via"], "sdk")
            self.assertIsNotNone(device._talk, "talk_start не пошёл в SDK")
            self.assertEqual(self.events(), ["login", S_ONE])
            await self.shutdown(device, entry)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
