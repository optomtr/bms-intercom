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


class ClockedCase(DeviceTestCase):
    """Часы callsource под управлением теста (asyncio живёт на настоящих)."""

    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        saved = callsource.time
        callsource.time = types.SimpleNamespace(monotonic=lambda: self.clock)
        self.addCleanup(setattr, callsource, "time", saved)

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


if __name__ == "__main__":
    unittest.main()
