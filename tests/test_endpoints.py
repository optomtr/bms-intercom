"""Endpoint selection, auth fallback and probe-report redaction.

Run: python3 -m unittest discover -s tests -v   (no pytest, no Home Assistant)
"""
from __future__ import annotations

import asyncio
import unittest

from _loader import load

endpoints = load("endpoints")
probe = load("probe")


def run(coro):
    return asyncio.run(coro)


class TestCandidateOrder(unittest.TestCase):
    """The DS-K1T341AM shape must be tried before the legacy one."""

    def test_rtsp_default_channel_first(self):
        self.assertEqual(
            endpoints.rtsp_paths(101),
            (
                "/Streaming/Channels/101",
                "/Streaming/Channels/102",
                "/Streaming/Channels/1",
            ),
        )

    def test_rtsp_configured_channel_wins_and_legacy_stays_last(self):
        paths = endpoints.rtsp_paths(102)
        self.assertEqual(paths[0], "/Streaming/Channels/102")
        self.assertEqual(paths[-1], "/Streaming/Channels/1")

    def test_rtsp_legacy_channel_keeps_the_modern_ones_as_fallback(self):
        paths = endpoints.rtsp_paths(1)
        self.assertEqual(paths[0], "/Streaming/Channels/1")
        self.assertIn("/Streaming/Channels/101", paths)

    def test_snapshot_picture_endpoint_is_first(self):
        self.assertEqual(
            endpoints.snapshot_paths(101)[0],
            "/ISAPI/Streaming/channels/101/picture",
        )

    def test_door_calls_xml_then_json(self):
        calls = endpoints.door_calls(2)
        self.assertEqual(calls[0].method, "PUT")
        self.assertEqual(
            calls[0].path, "/ISAPI/AccessControl/RemoteControl/door/2"
        )
        self.assertIn("<cmd>open</cmd>", calls[0].body)
        self.assertEqual(calls[0].content_type, "application/xml")
        self.assertTrue(calls[1].path.endswith("?format=json"))
        self.assertEqual(calls[1].content_type, "application/json")

    def test_rtsp_url_redacted_carries_no_credentials(self):
        url = endpoints.rtsp_url(
            "192.168.70.121", 554, "admin", "Sekret123!", "/Streaming/Channels/101",
            redacted=True,
        )
        self.assertNotIn("Sekret123", url)
        self.assertEqual(url, "rtsp://***@192.168.70.121:554/Streaming/Channels/101")


class TestSelectFirstWorking(unittest.TestCase):
    def test_picks_the_first_endpoint_that_answers(self):
        tried: list[str] = []

        async def attempt(path):
            tried.append(path)
            return path == "b"

        self.assertEqual(run(endpoints.select_first_working(["a", "b", "c"], attempt)), "b")
        self.assertEqual(tried, ["a", "b"])  # stops as soon as one works

    def test_a_raising_candidate_is_skipped(self):
        async def attempt(path):
            if path == "a":
                raise RuntimeError("404 shaped like an exception")
            return True

        self.assertEqual(run(endpoints.select_first_working(["a", "b"], attempt)), "b")

    def test_returns_none_when_nothing_answers(self):
        async def attempt(_path):
            return False

        self.assertIsNone(run(endpoints.select_first_working(["a", "b"], attempt)))

    def test_missing_endpoint_is_tolerated(self):
        for status in (400, 404, 405, 501):
            self.assertTrue(endpoints.tolerate(status), status)
        self.assertFalse(endpoints.tolerate(200))
        self.assertFalse(endpoints.tolerate(500))
        self.assertFalse(endpoints.tolerate(None))


class TestAuthFallback(unittest.TestCase):
    """Digest first (Hikvision default), basic only as a one-shot fallback."""

    def test_401_advertising_basic_switches_to_basic(self):
        self.assertEqual(
            endpoints.next_auth_mode("digest", 401, 'Basic realm="DS-K1T341AM"'),
            "basic",
        )

    def test_401_advertising_digest_means_wrong_credentials(self):
        self.assertIsNone(
            endpoints.next_auth_mode(
                "digest", 401, 'Digest qop="auth", realm="x", nonce="y"'
            )
        )

    def test_401_without_a_challenge_still_tries_basic_once(self):
        self.assertEqual(endpoints.next_auth_mode("digest", 401, None), "basic")

    def test_basic_never_falls_back_again(self):
        self.assertIsNone(endpoints.next_auth_mode("basic", 401, 'Basic realm="x"'))

    def test_success_does_not_switch_scheme(self):
        self.assertIsNone(endpoints.next_auth_mode("digest", 200, None))
        self.assertIsNone(endpoints.next_auth_mode("digest", 403, 'Basic realm="x"'))


class TestRedaction(unittest.TestCase):
    PASSWORD = "Sekret123!"

    def _results(self):
        return [
            probe.ProbeResult(
                name="deviceInfo",
                method="GET",
                url="http://192.168.70.121/ISAPI/System/deviceInfo",
                status=200,
                content_type="application/xml",
                body="<DeviceInfo><model>DS-K1T341AM</model></DeviceInfo>",
                auth="digest",
                secrets=(self.PASSWORD,),
            ),
            probe.ProbeResult(
                name="stream",
                method="GET",
                url=f"rtsp://admin:{self.PASSWORD}@192.168.70.121:554/Streaming/Channels/101",
                status=401,
                body=f"Authorization: Digest username=admin password={self.PASSWORD}",
                error=f"auth failed for http://admin:{self.PASSWORD}@192.168.70.121",
                secrets=(self.PASSWORD,),
            ),
        ]

    def test_report_never_leaks_the_password(self):
        report = probe.format_probe_report(
            self._results(), host="192.168.70.121",
            summary={"авторизация": "digest (успешно)"},
        )
        self.assertNotIn(self.PASSWORD, report)
        self.assertIn("rtsp://***@192.168.70.121", report)
        self.assertIn("***", report)

    def test_attributes_never_leak_the_password(self):
        attrs = probe.report_attributes(self._results(), host="192.168.70.121")
        self.assertNotIn(self.PASSWORD, attrs["probe_report"])
        for item in attrs["probe_results"]:
            self.assertNotIn(self.PASSWORD, repr(item))

    def test_report_states_status_and_auth_scheme(self):
        report = probe.format_probe_report(self._results(), host="192.168.70.121")
        self.assertIn("-> 200", report)
        self.assertIn("auth=digest", report)
        self.assertIn("[OK  ]", report)
        self.assertIn("[FAIL]", report)

    def test_body_snippet_is_cut_at_120_characters(self):
        long_body = "x" * 500
        text = endpoints.snippet(long_body)
        self.assertEqual(len(text), 121)  # 120 chars + the ellipsis
        self.assertTrue(text.endswith("…"))

    def test_redact_handles_any_scheme(self):
        self.assertEqual(
            endpoints.redact("http://admin:pw@host/x rtsp://u:p@host/y"),
            "http://***@host/x rtsp://***@host/y",
        )


if __name__ == "__main__":
    unittest.main()
