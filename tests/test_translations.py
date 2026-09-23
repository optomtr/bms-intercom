"""Every config-flow field must have a label in every language.

A missing key is not a crash — Home Assistant just shows the raw key
(`use_alert_stream`) in the dialog, which is what the owner saw. So it needs a
test rather than an eyeball.

Run: python3 -m unittest discover -s tests -v   (no pytest, no Home Assistant)
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "custom_components" / "bms_intercom"
FILES = {
    "strings.json": SRC / "strings.json",
    "ru": SRC / "translations" / "ru.json",
    "en": SRC / "translations" / "en.json",
}

# Fields the "real panel" step and the options step actually build.
REAL_FIELDS = {
    "name", "host", "username", "password", "http_port", "rtsp_port",
    "door_no", "channel", "use_alert_stream",
}
OPTION_FIELDS = {"proxy_port", "https_url", "call_timeout", "channel", "use_alert_stream"}
ERRORS = {"cannot_connect", "invalid_auth"}


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestTranslations(unittest.TestCase):
    def test_every_flow_field_has_a_label(self):
        for name, path in FILES.items():
            data = load_json(path)
            with self.subTest(file=name):
                self.assertEqual(
                    REAL_FIELDS, set(data["config"]["step"]["real"]["data"])
                )
                self.assertEqual(
                    OPTION_FIELDS, set(data["options"]["step"]["init"]["data"])
                )
                self.assertEqual(ERRORS, set(data["config"]["error"]))

    def test_config_flow_schema_matches_the_translations(self):
        """Keys used in config_flow.py must all be translated."""
        flow = (SRC / "config_flow.py").read_text(encoding="utf-8")
        const = (SRC / "const.py").read_text(encoding="utf-8")
        values = dict(
            re.findall(r'^(CONF_[A-Z_]+) = "([a-z_]+)"', const, re.MULTILINE)
        )
        used = {
            values[name]
            for name in re.findall(r"vol\.\w+\(\s*(CONF_[A-Z_]+)", flow)
            if name in values
        }
        labelled = set(load_json(FILES["ru"])["config"]["step"]["real"]["data"]) | set(
            load_json(FILES["ru"])["options"]["step"]["init"]["data"]
        )
        self.assertTrue(used, "не нашли ни одного поля в config_flow.py")
        self.assertEqual(used - labelled, set())

    def test_russian_labels_are_actually_russian_and_short(self):
        data = load_json(FILES["ru"])
        labels = {
            **data["config"]["step"]["real"]["data"],
            **data["options"]["step"]["init"]["data"],
        }
        self.assertEqual(labels["channel"], "Видеоканал")
        self.assertEqual(labels["use_alert_stream"], "Использовать поток событий")
        for key, text in labels.items():
            with self.subTest(field=key):
                self.assertNotEqual(text, key)          # not the raw key
                self.assertLessEqual(len(text), 40)     # fits the dialog
                if key not in ("host", "https_url"):
                    self.assertTrue(
                        re.search(r"[А-Яа-я]", text), f"{key}: {text}"
                    )

    def test_long_explanations_live_in_data_description(self):
        data = load_json(FILES["ru"])
        desc = data["config"]["step"]["real"]["data_description"]
        self.assertIn("101", desc["channel"])
        self.assertIn("alertStream", desc["use_alert_stream"])

    def test_probe_service_is_translated(self):
        for name, path in FILES.items():
            with self.subTest(file=name):
                services = load_json(path)["services"]
                self.assertIn("probe", services)
                self.assertEqual(
                    {"entry_id", "test_door", "minutes"}, set(services["probe"]["fields"])
                )

    def test_service_yaml_and_translations_agree(self):
        text = (SRC / "services.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("probe:"))
        for field in ("entry_id", "test_door", "minutes"):
            self.assertIn(f"{field}:", text)


if __name__ == "__main__":
    unittest.main()
