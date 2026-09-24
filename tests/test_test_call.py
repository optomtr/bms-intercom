"""«Тестовый звонок» (0.3.2): настоящий звонок с панели или запасной режим.

Фейковая панель как в test_device.py (класс Panel из test_ds_k1t341am), плюс
callSignal и keyCfg. Сценарии:
  (а) панель принимает request и начинает отдавать ringing -> настоящий звонок;
  (б) панель отвечает 4xx/notSupport -> запасной режим, ответ/сброс НЕ шлют
      callSignal на панель;
  (в) панель ответила OK, но ringing не пришёл -> запасной режим;
  (г) опрос со словом idle не гасит тестовый вызов раньше срока;
  модель без callSignal (профиль DS-K1T341AM) -> сразу запасной, без попыток.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from _loader import load
from test_device import HAVE_HA, DeviceTestCase

endpoints = load("endpoints")
if HAVE_HA:
    import httpx

    callsource = load("callsource")
    testcall = load("testcall")
    from test_ds_k1t341am import Panel

    OK_JSON = '{"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"}'
    NOT_SUPPORT_JSON = (
        '{"statusCode": 4, "statusString": "Invalid Operation", '
        '"subStatusCode": "notSupport"}'
    )

    class CallPanel(Panel):
        """Панель с callSignal. mode: rings | refuse | silent_ok."""

        def __init__(self, mode: str):
            super().__init__("idle")
            self.mode = mode
            self.signals: list[str] = []      # тела всех PUT callSignal

        def __call__(self, request):
            path = request.url.path
            if path == "/ISAPI/VideoIntercom/capabilities":
                self.paths.append(path)
                return httpx.Response(
                    200, headers={"Content-Type": "application/json"},
                    text='{"VideoIntercomCap": {"CallSignal": {"cmdType": '
                         '["request", "answer", "reject", "hangUp"]}}}',
                )
            if path == "/ISAPI/VideoIntercom/keyCfg/1":
                self.paths.append(path)
                return httpx.Response(
                    200, headers={"Content-Type": "application/xml"},
                    text="<KeyCfg><id>1</id><module>main</module>"
                         "<callNumber>7</callNumber></KeyCfg>",
                )
            if path == "/ISAPI/VideoIntercom/callSignal" and request.method == "PUT":
                body = request.content.decode()
                self.signals.append(body)
                if "request" not in body:
                    return httpx.Response(200, text=OK_JSON)   # answer/reject
                if self.mode == "refuse":
                    return httpx.Response(400, text=NOT_SUPPORT_JSON)
                if self.mode == "rings":
                    self.status = "ring"
                return httpx.Response(200, text=OK_JSON)
            return super().__call__(request)


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class TestCallCase(DeviceTestCase):
    def setUp(self):
        super().setUp()
        for mod, name, value in (
            (testcall, "TEST_CALL_WAIT_SECONDS", 1.0),
            # Окно звонка тоже держит ringing при idle — обнуляем, чтобы (г)
            # проверял именно защиту теста, а не окно.
            (callsource, "RING_WINDOW_SECONDS", 0),
        ):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    async def start(self, panel):
        """Устройство на опросе; профиль (capabilities) уже прочитан."""
        self.use_panel(panel)
        device, _hass, entry = self.make_device(options={"use_alert_stream": False})
        await device.async_setup()
        await self.settle(entry, lambda: device.signal_supported is not None)
        return device, entry

    async def press(self, device):
        """Нажать кнопку, пока опрос callStatus тикает, как в HA, каждые 20 мс."""
        async def ticker():
            while True:
                await self.sched.tick()
                await asyncio.sleep(0.02)

        task = asyncio.get_running_loop().create_task(ticker())
        try:
            await device.async_test_call()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestRealCall(TestCallCase):
    def test_a_panel_that_rings_is_a_real_call(self):
        panel = CallPanel("rings")

        async def main():
            device, entry = await self.start(panel)
            await self.press(device)
            self.assertEqual(device.call_state, "ringing")
            self.assertFalse(device._test_call)
            self.assertEqual(device.call_source, "опрос callStatus")
            self.assertIn("настоящий звонок", device.test_call_result)
            # Адресат — комната из keyCfg (callNumber 7), команда — request.
            first = json.loads(panel.signals[0])["CallSignal"]
            self.assertEqual(first["cmdType"], "request")
            self.assertEqual(first["target"]["roomNumber"], 7)
            # Настоящий звонок: «Ответить» идёт на панель как обычно.
            await device.async_answer()
            self.assertIn("answer", panel.signals[-1])
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestFallback(TestCallCase):
    def test_refusal_gives_a_local_call_that_never_signals_the_panel(self):
        panel = CallPanel("refuse")

        async def main():
            device, entry = await self.start(panel)
            await self.press(device)
            self.assertEqual(device.call_state, "ringing")
            self.assertEqual(device.call_source, "тест")
            self.assertIn("запасной режим", device.test_call_result)
            self.assertIn("notSupport", device.test_call_result)
            self.assertEqual(len(panel.signals), 3)     # все формы request
            panel.signals.clear()
            await device.async_answer()
            self.assertEqual(device.call_state, "answered")
            await device.async_reject()
            self.assertEqual(device.call_state, "idle")
            self.assertEqual(panel.signals, [], "тест слал callSignal на панель")
            self.assertEqual(device.call_source, "опрос callStatus")
            # Ответ/сброс не посчитаны отказом панели.
            self.assertIs(device.signal_supported, True)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_ok_without_ringing_is_not_a_real_call(self):
        panel = CallPanel("silent_ok")
        testcall.TEST_CALL_WAIT_SECONDS = 0.3

        async def main():
            device, entry = await self.start(panel)
            await self.press(device)
            self.assertEqual(len(panel.signals), 1)     # OK с первой формы
            self.assertEqual(device.call_state, "ringing")
            self.assertEqual(device.call_source, "тест")
            self.assertIn("не появился", device.test_call_result)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_model_without_call_signal_goes_straight_to_fallback(self):
        panel = Panel("idle")          # DS-K1T341AM V3.2.30: callSignal нет

        async def main():
            device, entry = await self.start(panel)
            self.assertIs(device.signal_supported, False)
            panel.paths.clear()
            await device.async_test_call()
            self.assertEqual(device.call_state, "ringing")
            self.assertIn("нет команд вызова", device.test_call_result)
            self.assertFalse(
                [p for p in panel.paths if "callSignal" in p or "keyCfg" in p]
            )
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestPollDoesNotEndTheTest(TestCallCase):
    def test_idle_polls_keep_the_test_call_until_its_deadline(self):
        panel = CallPanel("refuse")

        async def main():
            device, entry = await self.start(panel)
            await self.press(device)
            await self.sched.tick(5)                    # панель: idle, idle, …
            self.assertEqual(device.call_state, "ringing")
            self.assertEqual(device.call_source, "тест")
            device._test_call_until = 0                 # срок теста вышел
            await self.sched.tick()
            self.assertEqual(device.call_state, "idle")
            self.assertFalse(device._test_call)
            await self.shutdown(device, entry)

        asyncio.run(main())

    def test_a_real_ring_takes_over_the_test_call(self):
        panel = CallPanel("refuse")

        async def main():
            device, entry = await self.start(panel)
            await self.press(device)
            panel.status = "ring"                       # пришёл посетитель
            await self.sched.tick()
            self.assertFalse(device._test_call)
            self.assertEqual(device.call_source, "опрос callStatus")
            panel.signals.clear()
            await device.async_answer()
            self.assertIn("answer", panel.signals[-1])  # теперь — на панель
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestResponseParsing(unittest.TestCase):
    def test_body_ok_reads_the_status_code(self):
        self.assertTrue(endpoints.body_ok(""))
        self.assertTrue(endpoints.body_ok('{"statusCode": 1, "subStatusCode": "ok"}'))
        self.assertTrue(endpoints.body_ok("<statusCode>1</statusCode>"))
        self.assertFalse(endpoints.body_ok('{"statusCode":"4"}'))
        self.assertFalse(
            endpoints.body_ok("<subStatusCode>x</subStatusCode><statusCode>4</statusCode>")
        )

    def test_call_number_and_refusal(self):
        self.assertEqual(endpoints.parse_call_number("<callNumber>12</callNumber>"), 12)
        self.assertEqual(endpoints.parse_call_number('{"callNumber": "3"}'), 3)
        self.assertIsNone(endpoints.parse_call_number("<callNumber>10010110001</callNumber>"))
        self.assertIsNone(endpoints.parse_call_number(None))
        self.assertEqual(endpoints.refusal_word(NOT_SUPPORT), "notSupport")
        self.assertEqual(
            endpoints.refusal_word('{"subStatusCode": "Unknow", "errorMsg": "noRequest"}'),
            "noRequest",
        )


NOT_SUPPORT = '{"statusCode": 4, "subStatusCode": "notSupport"}'


if __name__ == "__main__":
    unittest.main()
