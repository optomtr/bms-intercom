"""Окно звонка при постоянном опросе callStatus (0.3.2).

На объекте кнопка вызова DS-K1T341AM давала ring лишь на 1–2 с, в alertStream
не приходило ничего, а мы при подключённом потоке опрос останавливали — поп-ап
не появлялся. Здесь время callsource подменено, чтобы проверить окно в 25 с
без настоящего ожидания.

Needs Home Assistant importable (the prepared venv); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import types
import unittest

from test_device import HAVE_HA, DeviceTestCase, NeverEndingBody

if HAVE_HA:
    import httpx

    from _loader import load
    from test_ds_k1t341am import Panel

    callsource = load("callsource")
    device_mod = load("device")


class ClockedCase(DeviceTestCase):
    """Часы callsource под управлением теста (asyncio живёт на настоящих)."""

    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        fake = types.SimpleNamespace(monotonic=lambda: self.clock)
        for module in (callsource, device_mod):
            self.addCleanup(setattr, module, "time", module.time)
            module.time = fake

    def silent_stream_panel(self, panel):
        """Поток событий подключается и молчит, как у терминала на объекте."""

        def handler(request):
            if "alertStream" in request.url.path:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "multipart/mixed; boundary=b"},
                    stream=NeverEndingBody(),
                )
            return panel(request)

        self.use_panel(handler)

    async def poll_at(self, seconds, status=None):
        """Опрос в момент «через `seconds` с после старта теста»."""
        self.clock = 1000.0 + seconds
        if status is not None:
            self.panel.status = status
        await self.sched.tick()


class TestShortRingWithStream(ClockedCase):
    def test_short_ring_holds_the_popup_while_the_stream_is_silent(self):
        self.panel = Panel("idle")
        self.silent_stream_panel(self.panel)

        async def main():
            device, _hass, entry = self.make_device()
            await device.async_setup()
            connected = await self.settle(
                entry, lambda: device.client.alert_stream_supported is True
            )
            self.assertTrue(connected, "поток событий не подключился")
            await self.poll_at(0, "ring")          # ring ровно на один опрос
            self.assertEqual(device.call_state, "ringing", "короткий ring не пойман")
            self.assertEqual(device.call_source, "поток событий + опрос")
            for second in range(1, 25):
                await self.poll_at(second, "idle")
                self.assertEqual(device.call_state, "ringing", f"погас на {second} с")
            device._apply_panel_state("idle")      # idle из потока — тоже не гасит
            self.assertEqual(device.call_state, "ringing")
            await self.poll_at(26)
            self.assertEqual(device.call_state, "idle")
            await self.shutdown(device, entry)

        asyncio.run(main())


class TestNoRingAgainAfterHangUp(ClockedCase):
    """На объекте: «Сбросить» → поп-ап тут же снова «Входящий вызов», по кругу.
    Опрос ещё видел ring и засчитывал его как новый звонок."""

    def run_poll_device(self, scenario):
        self.panel = Panel("idle")
        self.use_panel(self.panel)

        async def main():
            device, _hass, entry = self.make_device(options={"use_alert_stream": False})
            await device.async_setup()
            await self.poll_at(0, "ring")
            self.assertEqual(device.call_state, "ringing")
            await scenario(device)
            await self.shutdown(device, entry)

        asyncio.run(main())

    async def assert_ring_is_ignored_then_a_new_call_works(self, device):
        for second in (6, 7, 8):                   # панель ещё звонит
            await self.poll_at(second, "ring")
            self.assertEqual(device.call_state, "idle", f"поп-ап вернулся на {second} с")
        device._apply_panel_state("ringing")       # и из потока — тоже нет
        self.assertEqual(device.call_state, "idle")
        await self.poll_at(9, "idle")
        await self.poll_at(25, "ring")             # новый посетитель через 20 с
        self.assertEqual(device.call_state, "ringing", "новый звонок не открылся")

    def test_reject_then_the_panel_still_rings(self):
        async def scenario(device):
            self.clock = 1005.0
            await device.async_reject()
            self.assertEqual(device.call_state, "idle")
            await self.assert_ring_is_ignored_then_a_new_call_works(device)

        self.run_poll_device(scenario)

    def test_door_opened_then_the_card_ends_the_call(self):
        async def scenario(device):
            # Карточка: «Открыть» во время звонка = ответ + дверь, через 5 с
            # сама жмёт «Сбросить».
            await device.async_answer()
            await device.async_open_door()
            self.assertEqual(self.panel.door_opened, 1)
            self.clock = 1005.0
            await device.async_reject()
            self.assertEqual(device.call_state, "idle")
            await self.assert_ring_is_ignored_then_a_new_call_works(device)

        self.run_poll_device(scenario)


if __name__ == "__main__":
    unittest.main()
