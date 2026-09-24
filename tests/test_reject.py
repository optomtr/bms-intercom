"""«Сбросить» шлёт панели обе команды: reject, затем hangUp (0.3.3).

С живого терминала: после «Ответить» кнопка «Сбросить» поп-апа НЕ гасила
звонок на терминале, а «Сбросить» без ответа — гасила. Причина: в разговоре
уходил только hangUp (терминал отвечал 200, цикл на этом останавливался), а
экран «звоню» терминал снимает лишь по reject. Теперь — всегда обе, по порядку,
и сбой одной не мешает другой.
"""
from __future__ import annotations

import asyncio
import re
import unittest

from test_device import HAVE_HA, DeviceTestCase

if HAVE_HA:
    import httpx

    from test_test_call import CallPanel


_CMD = re.compile(r'cmdType"?\s*[:>]\s*"?(\w+)')


def sent_commands(panel) -> list[str]:
    """cmdType каждого PUT callSignal по порядку (JSON- и XML-форма)."""
    return [_CMD.search(body).group(1) for body in panel.signals]


class RejectCase(DeviceTestCase):
    async def start(self, panel):
        self.use_panel(panel)
        device, _hass, entry = self.make_device(options={"use_alert_stream": False})
        await device.async_setup()
        await self.settle(entry, lambda: device.signal_supported is not None)
        self.assertIs(device.signal_supported, True)
        return device, entry

    def reject_from(self, panel, state):
        async def main():
            device, entry = await self.start(panel)
            if state == "answered":
                await device.async_answer()
            else:
                device._apply_panel_state("ringing")
            self.assertEqual(device.call_state, state)
            panel.signals.clear()
            await device.async_reject()
            result = (sent_commands(panel), device.call_state, device.panel_available)
            await self.shutdown(device, entry)
            return result

        return asyncio.run(main())

    def test_answered_sends_reject_then_hang_up(self):
        cmds, state, available = self.reject_from(CallPanel("rings"), "answered")
        self.assertEqual(cmds, ["reject", "hangUp"])
        self.assertEqual((state, available), ("idle", True))

    def test_ringing_sends_both_too(self):
        cmds, state, _ = self.reject_from(CallPanel("rings"), "ringing")
        self.assertEqual(cmds, ["reject", "hangUp"])
        self.assertEqual(state, "idle")

    def test_failed_reject_does_not_stop_hang_up(self):
        class RejectBroken(CallPanel):
            def __call__(self, request):
                if b"reject" in request.content:
                    self.signals.append(request.content.decode())
                    return httpx.Response(500, text="boom")
                return super().__call__(request)

        cmds, state, available = self.reject_from(RejectBroken("rings"), "answered")
        self.assertEqual(cmds[-1], "hangUp")
        self.assertIn("reject", cmds)
        self.assertEqual((state, available), ("idle", True))

    def test_both_refused_is_a_failure(self):
        class AllBroken(CallPanel):
            def __call__(self, request):
                if b"reject" in request.content or b"hangUp" in request.content:
                    self.signals.append(request.content.decode())
                    return httpx.Response(500, text="boom")
                return super().__call__(request)

        cmds, state, available = self.reject_from(AllBroken("rings"), "answered")
        self.assertEqual(sorted(set(cmds)), ["hangUp", "reject"])
        self.assertIs(available, False)
        self.assertEqual(state, "answered")


if __name__ == "__main__":
    unittest.main()
