"""The popup card must survive the states the real frontend goes through.

Reported from Chrome (Android + Windows):
    TypeError: Cannot convert undefined or null to object
      at groupIntercoms (bms_intercom_card.js:32)  <- tick (…:256)
`hass` exists while the frontend boots / reconnects its websocket, but
`hass.states` does not yet. Runs tests/js/card_harness.js under node.

Run: python3 -m unittest discover -s tests -v   (skipped when node is absent)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

try:
    import homeassistant  # noqa: F401
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

from _loader import SRC, load

if HAVE_HA:
    integration = load("__init__")
    cardversion = load("cardversion")
    entity_mod = load("entity")
    from test_device import DeviceTestCase
else:  # pragma: no cover
    DeviceTestCase = unittest.TestCase

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

    def test_operator_speech_ducks_the_panel_then_releases(self):
        """Эхо с объекта: пока оператор говорит — звук панели 0.12, тишина дольше удержания — 1.0."""
        self.assertEqual(self.assertSurvives("duck_speech"),
                         {"quiet": 1, "s1": 1, "talk": 0.12, "held": 0.12, "back": 1})

    def test_mic_off_never_ducks(self):
        self.assertEqual(self.assertSurvives("duck_mic_off"), {"off": 1, "offMid": 1})

    def test_steady_room_noise_is_not_endless_speech(self):
        """Порог = max(base, фон×3): ровный шум уходит в фон, речь над ним всё равно глушит."""
        self.assertEqual(self.assertSurvives("duck_noisy_room"), {"noise": 1, "voice": 0.12})

    def test_duck_chain_plays_once_and_is_torn_down(self):
        """Звук панели WebRTC → GainNode при микрофоне; <video> немой (без удвоения);
        микрофон выкл/закрытие поп-апа снимают узлы и AudioContext; без WebAudio — как раньше."""
        got = self.assertSurvives("duck_wired")
        self.assertFalse(got["beforeMic"], "до микрофона звук панели должен играть <video>")
        self.assertTrue(got["routedMuted"], "<video> играет параллельно с WebAudio — звук двойной")
        self.assertTrue(got["toSpeaker"], "GainNode не выведен в динамик")
        self.assertEqual((got["speech"], got["after"]), (0.12, 1))
        self.assertEqual(got["micOff"], {"muted": False, "ctx": "closed", "src": 0, "timer": True})
        self.assertEqual(got["closed"], {"ctx": "closed", "src": 0, "gain": 0, "timer": True, "muted": True})
        self.assertEqual(got["warnings"], 0)
        self.assertFalse(got["noWebAudioMuted"], "без WebAudio звук панели пропал")

    # --- 0.3.10: вкладка со старым JS перезагружается сама ------------------
    def test_stale_card_reloads_once_after_5s_idle(self):
        """На объекте старая вкладка играла ring1.mp3, новая — velvet, разом."""
        self.assertEqual(self.assertSurvives("stale_reload_once"),
                         {"version": "old111", "early": 0, "at5": 1, "later": 1,
                          "lines": 1, "warns": 0})

    def test_stale_card_never_reloads_during_a_call_or_view(self):
        """Звонок, разговор, просмотр — никогда; после звонка 5 с отсчёта заново."""
        self.assertEqual(self.assertSurvives("stale_busy_never"),
                         {"ringing": 0, "answered": 0, "view": 0,
                          "afterCallEarly": 0, "afterCall": 1})

    def test_same_version_does_not_reload(self):
        self.assertEqual(self.assertSurvives("stale_same_version"), 0)

    def test_attribute_missing_does_not_reload(self):
        self.assertEqual(self.assertSurvives("stale_attr_missing"), 0)

    def test_same_target_within_10_min_is_not_retried(self):
        """Reload отдал тот же старый файл — не петля; новая цель или 10 мин — можно."""
        self.assertEqual(self.assertSurvives("stale_repeat_guard"),
                         {"first": 1, "again": 0, "after10min": 1, "newTarget": 1})

    def test_session_storage_off_falls_back_to_local_storage(self):
        self.assertEqual(self.assertSurvives("stale_session_off_local_on"),
                         {"first": 1, "lines": 1, "again": 0})

    def test_no_storage_at_all_never_reloads(self):
        """Негде запомнить попытку → петля возможна → только «обновите вручную», один раз."""
        self.assertEqual(self.assertSurvives("stale_no_storage"),
                         {"reloads": 0, "manual": 1, "warns": 0})

    def test_unknown_own_version_or_kiosk_never_reloads(self):
        self.assertEqual(self.assertSurvives("stale_unknown_version"),
                         {"plain": 0, "plainVersion": "", "noV": 0, "kiosk": 0})

    def test_es_module_finds_its_version_without_current_script(self):
        """Фронтенд HA грузит карточку модулем: currentScript = null."""
        self.assertEqual(self.assertSurvives("stale_module_url_from_stack"),
                         {"version": "mod777", "reloads": 1})

    def test_a_failing_tick_is_logged_once_not_every_400ms(self):
        self.assertSurvives("safe_tick_swallows")
        self.assertEqual(self.out["warnings"], 1)
        self.assertEqual(self.out["errors"], 0)


class _FrontendHass:
    """Ровно то, что трогает _async_register_frontend."""

    def __init__(self):
        self.data = {}

        class _Http:
            async def async_register_static_paths(self, paths):
                return None

        self.http = _Http()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@unittest.skipUnless(HAVE_HA, "Home Assistant not installed")
class TestCardVersionAttribute(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self._saved_version = cardversion._version
        cardversion._version = None
        self._saved_add = integration.add_extra_js_url

    def tearDown(self):
        integration.add_extra_js_url = self._saved_add
        cardversion._version = self._saved_version
        super().tearDown()

    def attrs(self, device):
        return entity_mod.BMSIntercomEntity(device, "k").intercom_attributes

    def test_attribute_equals_the_hash_in_the_card_url(self):
        device, _hass, _entry = self.make_device()
        # До регистрации фронтенда метки нет — атрибута нет, карточка молчит.
        self.assertNotIn("intercom_card_version", self.attrs(device))
        urls = []
        integration.add_extra_js_url = lambda hass, url: urls.append(url)
        asyncio.run(integration._async_register_frontend(_FrontendHass()))
        self.assertEqual(len(urls), 1)
        url_version = parse_qs(urlparse(urls[0]).query)["v"][0]
        body = (SRC / "frontend" / "bms_intercom_card.js").read_bytes()
        self.assertEqual(url_version, hashlib.md5(body).hexdigest()[:10])
        # Атрибут пишется на каждый state — файл при этом больше не читается.
        with mock.patch("builtins.open", side_effect=AssertionError("файл читается на state")):
            got = self.attrs(device)
        self.assertEqual(got["intercom_card_version"], url_version)

    def test_ringtone_attribute_points_to_a_bundled_file(self):
        # Планшет качает мелодию по intercom_ringtone: путь должен вести на
        # реальный файл в frontend/, иначе он молча доиграет прошлую мелодию.
        device, _hass, _entry = self.make_device()
        path = self.attrs(device)["intercom_ringtone"]
        self.assertTrue(path.startswith("/bms_intercom_static/"), path)
        self.assertTrue((SRC / "frontend" / path.rsplit("/", 1)[1]).is_file(), path)


if __name__ == "__main__":
    unittest.main()
