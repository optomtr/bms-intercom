"""The popup card must survive the states the real frontend goes through.

Reported from Chrome (Android + Windows):
    TypeError: Cannot convert undefined or null to object
      at groupIntercoms (bms_intercom_card.js:32)  <- tick (…:256)
`hass` exists while the frontend boots / reconnects its websocket, but
`hass.states` does not yet. Runs tests/js/card_harness.js under node.

Run: python3 -m unittest discover -s tests -v   (skipped when node is absent)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "js" / "card_harness.js"
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node not installed")
class TestPopupCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [NODE, str(HARNESS)], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            raise AssertionError(f"harness crashed: {proc.stderr[-2000:]}")
        cls.out = json.loads(proc.stdout)

    def assertSurvives(self, case):
        result = self.out[case]
        self.assertTrue(result["ok"], f"{case}: {result.get('error')}")
        return result["value"]

    def test_no_home_assistant_element(self):
        self.assertSurvives("no_home_assistant")

    def test_hass_without_states_does_not_throw(self):
        """The exact crash from the field."""
        self.assertSurvives("hass_without_states")
        self.assertEqual(self.assertSurvives("group_without_states"), 0)

    def test_states_null(self):
        self.assertSurvives("states_null")

    def test_null_entry_inside_states(self):
        self.assertEqual(self.assertSurvives("null_entry"), 0)

    def test_a_ringing_call_is_still_found(self):
        self.assertEqual(self.assertSurvives("ringing_group"), "ringing")

    def test_camera_without_token_yet(self):
        self.assertSurvives("ringing_tick_without_camera_token")

    def test_shown_toast_takes_clicks(self):
        """0.3.3: ссылка «Открыть по HTTPS» в тосте не нажималась."""
        self.assertTrue(self.assertSurvives("toast_show_takes_clicks"))

    def test_terminal_without_two_way_audio_has_no_mic(self):
        """0.3.3, DS-K1T341AM: talk_supported=no → нет «Микрофона», нет захвата."""
        got = self.assertSurvives("talk_no")
        self.assertTrue(got["hidden"], "кнопка «Микрофон» видна")
        self.assertEqual(got["gum"], 0, "getUserMedia вызван")
        self.assertFalse(got["talkStart"], "talk_start отправлен")
        self.assertEqual(got["first"], "Терминал не принимает голос с HA — только слушать")
        self.assertEqual(got["second"], "", "тост повторяется на каждый ответ")

    def test_terminal_with_two_way_audio_keeps_the_mic(self):
        """Контроль к предыдущему: та же проверка видит работающий микрофон."""
        self.assertEqual(self.assertSurvives("talk_yes"), {"hidden": False, "gum": 1})

    def test_no_voice_with_a_hint_says_why_once(self):
        """0.3.5: talk_supported=no + talk_hint → тост с причиной, один раз."""
        got = self.assertSurvives("talk_hint")
        self.assertTrue(got["hidden"], "кнопка «Микрофон» видна")
        self.assertEqual(got["first"], "Только слушать: голос по SDK недоступен на этой платформе")
        self.assertEqual(got["second"], "", "тост повторяется на каждый ответ")

    def test_voice_through_the_sdk_keeps_the_mic(self):
        """0.3.5: talk_via=sdk (DS-K1T341AM через HCNetSDK) — микрофон как у ISAPI."""
        self.assertEqual(self.assertSurvives("talk_sdk"), {"hidden": False, "gum": 1})

    def test_a_failing_tick_is_logged_once_not_every_400ms(self):
        self.assertSurvives("safe_tick_swallows")
        self.assertEqual(self.out["warnings"], 1)
        self.assertEqual(self.out["errors"], 0)


if __name__ == "__main__":
    unittest.main()
