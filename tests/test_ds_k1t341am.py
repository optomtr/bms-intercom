"""The real capability profile of DS-K1T341AM V3.2.30, as probed on site.

OK:   userCheck, deviceInfo(+json), Streaming/channels/101, /102,
      VideoIntercom/callStatus?format=json -> {"CallStatus":{"status":"idle"}},
      VideoIntercom/capabilities?format=json
404:  System/status, Streaming/channels/1, every snapshot path,
      VideoIntercom/callSignal, door/1/capabilities (GET not allowed),
      Event/notification/alertStream   <- no event stream on this firmware
PUT:  AccessControl/RemoteControl/door/1 works (only tried on demand)

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import unittest

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from _loader import load

if httpx is not None:
    isapi = load("isapi")
    probe = load("probe")
    events = load("events")

CAPABILITIES = (
    '{"VideoIntercomCap": {"version": "2.0", "isSupportCallStatus": true, '
    '"isSupportUnlock": true}}'
)
NOT_FOUND = (
    '<ResponseStatus><statusCode>4</statusCode>'
    '<statusString>Invalid Operation</statusString>'
    '<subStatusCode>notSupport</subStatusCode></ResponseStatus>'
)
METHOD_NOT_ALLOWED = (
    '<ResponseStatus><statusCode>4</statusCode>'
    '<statusString>Invalid Operation</statusString>'
    '<subStatusCode>methodNotAllowed</subStatusCode></ResponseStatus>'
)


class Panel:
    """DS-K1T341AM as measured, minus the digest layer (tested elsewhere)."""

    def __init__(self, status: str = "idle"):
        self.status = status
        self.door_opened = 0
        self.paths: list[str] = []

    def __call__(self, request: "httpx.Request") -> "httpx.Response":
        path = request.url.path
        query = request.url.query.decode()
        self.paths.append(path + (f"?{query}" if query else ""))
        method = request.method

        if method == "PUT" and path == "/ISAPI/AccessControl/RemoteControl/door/1":
            self.door_opened += 1
            return httpx.Response(
                200, headers={"Content-Type": "application/xml"},
                text="<ResponseStatus><statusCode>1</statusCode>"
                     "<statusString>OK</statusString></ResponseStatus>",
            )
        if method != "GET":
            return httpx.Response(404, text=NOT_FOUND)

        if path == "/ISAPI/Security/userCheck":
            return httpx.Response(
                200, headers={"Content-Type": "application/xml"},
                text="<userCheck><statusValue>200</statusValue>"
                     "<statusString>OK</statusString></userCheck>",
            )
        if path == "/ISAPI/System/deviceInfo":
            return httpx.Response(
                200, headers={"Content-Type": "application/xml"},
                text="<DeviceInfo><model>DS-K1T341AM</model>"
                     "<firmwareVersion>V3.2.30</firmwareVersion></DeviceInfo>",
            )
        if path in (
            "/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102"
        ):
            return httpx.Response(
                200, headers={"Content-Type": "application/xml"},
                text="<StreamingChannel><id>%s</id></StreamingChannel>"
                     % path.rsplit("/", 1)[-1],
            )
        if path == "/ISAPI/VideoIntercom/callStatus":
            # Both the json and the bare path answer the same JSON body.
            return httpx.Response(
                200, headers={"Content-Type": "application/json"},
                text='{"CallStatus": {"status": "%s"}}' % self.status,
            )
        if path == "/ISAPI/VideoIntercom/capabilities":
            return httpx.Response(
                200, headers={"Content-Type": "application/json"}, text=CAPABILITIES
            )
        if path.endswith("/capabilities") and "RemoteControl/door" in path:
            return httpx.Response(405, text=METHOD_NOT_ALLOWED)
        return httpx.Response(404, text=NOT_FOUND)


def run(coro):
    return asyncio.run(coro)


def make_client(panel, **kwargs):
    client = isapi.ISAPIClient("192.168.70.121", "admin", "Sekret123!", **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(panel))
    return client


@unittest.skipIf(httpx is None, "httpx not installed")
class TestCallStatusPolling(unittest.TestCase):
    """No event stream here — the poll is the doorbell."""

    def test_idle_body_is_parsed(self):
        client = make_client(Panel("idle"))
        self.assertEqual(run(client.async_get_call_status()), events.STATE_IDLE)
        run(client.async_close())

    def test_every_status_word_of_this_family(self):
        for word, expected in (
            ("idle", events.STATE_IDLE),
            ("ring", events.STATE_RINGING),
            ("ringing", events.STATE_RINGING),
            ("calling", events.STATE_RINGING),
            ("onCall", events.STATE_ANSWERED),
            ("talking", events.STATE_ANSWERED),
            ("answered", events.STATE_ANSWERED),
            ("hangUp", events.STATE_IDLE),
            ("endCall", events.STATE_IDLE),
        ):
            with self.subTest(status=word):
                client = make_client(Panel(word))
                self.assertEqual(run(client.async_get_call_status()), expected)
                run(client.async_close())

    def test_unknown_word_returns_none_so_the_state_is_kept(self):
        client = make_client(Panel("somethingNew"))
        self.assertIsNone(run(client.async_get_call_status()))
        run(client.async_close())

    def test_unknown_word_is_logged_once(self):
        panel = Panel("somethingNew")
        client = make_client(panel)
        run(client.async_get_call_status())
        run(client.async_get_call_status())
        self.assertEqual(client._unknown_status_logged, {"somethingNew"})
        run(client.async_close())

    def test_the_json_endpoint_is_chosen_and_remembered(self):
        panel = Panel()
        client = make_client(panel)
        run(client.async_get_call_status())
        panel.paths.clear()
        run(client.async_get_call_status())
        self.assertEqual(
            panel.paths, ["/ISAPI/VideoIntercom/callStatus?format=json"]
        )
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestNoAlertStream(unittest.TestCase):
    def test_stream_raises_unsupported_and_is_remembered(self):
        client = make_client(Panel())

        async def drain():
            async for _event in client.async_iter_alerts():
                pass

        with self.assertRaises(isapi.ISAPIUnsupported):
            run(drain())
        self.assertIs(client.alert_stream_supported, False)
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestNoSnapshot(unittest.TestCase):
    def test_snapshot_is_tried_once_then_never_again(self):
        panel = Panel()
        client = make_client(panel)
        self.assertIsNone(run(client.async_snapshot()))
        self.assertIs(client.snapshot_supported, False)
        tried = len([p for p in panel.paths if "picture" in p or "httpPreview" in p])
        self.assertGreater(tried, 0)
        panel.paths.clear()
        self.assertIsNone(run(client.async_snapshot()))
        self.assertEqual(panel.paths, [])      # no 404 storm on every frame
        run(client.async_close())

    def test_rtsp_stream_is_still_available(self):
        client = make_client(Panel())
        self.assertEqual(
            run(client.async_select_channel()), "/Streaming/Channels/101"
        )
        self.assertIn("/Streaming/Channels/101", client.rtsp_url())
        self.assertNotIn("Sekret123", client.rtsp_url(redacted=True))
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestAnswerReject(unittest.TestCase):
    def test_capabilities_say_there_is_no_call_signal(self):
        client = make_client(Panel())
        self.assertFalse(run(client.async_load_capabilities()))
        self.assertIs(client.call_signal_supported, False)
        run(client.async_close())

    def test_answer_is_a_no_op_without_touching_the_network(self):
        panel = Panel()
        client = make_client(panel)
        run(client.async_load_capabilities())
        panel.paths.clear()
        self.assertFalse(run(client.async_answer()))   # no raise
        self.assertFalse(run(client.async_reject()))
        self.assertEqual(panel.paths, [])
        run(client.async_close())

    def test_a_panel_that_does_have_call_signal_is_detected(self):
        class WithSignal(Panel):
            def __call__(self, request):
                if request.url.path == "/ISAPI/VideoIntercom/capabilities":
                    return httpx.Response(
                        200, headers={"Content-Type": "application/json"},
                        text='{"VideoIntercomCap": {"CallSignal": {"cmdType": '
                             '["answer", "reject"]}}}',
                    )
                return super().__call__(request)

        client = make_client(WithSignal())
        self.assertTrue(run(client.async_load_capabilities()))
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestDoor(unittest.TestCase):
    def test_xml_put_is_the_primary_and_it_works(self):
        panel = Panel()
        client = make_client(panel)
        run(client.async_open_door())
        self.assertEqual(panel.door_opened, 1)
        self.assertIn("/ISAPI/AccessControl/RemoteControl/door/1", panel.paths)
        run(client.async_close())

    def test_probe_does_not_open_the_door(self):
        panel = Panel()
        client = make_client(panel)
        _results, summary = run(client.async_probe())
        self.assertEqual(panel.door_opened, 0)
        self.assertIn("не трогали", summary["реле двери"])
        run(client.async_close())

    def test_opt_in_probe_reports_the_put_status(self):
        panel = Panel()
        client = make_client(panel)
        _results, summary = run(client.async_probe(test_door=True))
        self.assertEqual(panel.door_opened, 1)
        self.assertIn("ОТКРЫТА", summary["реле двери"])
        self.assertIn("200", summary["реле двери"])
        run(client.async_close())

    def test_capabilities_405_does_not_condemn_the_put(self):
        """GET on door capabilities is 405 — the PUT must still be tried."""
        panel = Panel()
        client = make_client(panel)
        results, _summary = run(client.async_probe())
        caps = [r for r in results if r.name == "door.capabilities"][0]
        self.assertFalse(caps.ok)
        run(client.async_open_door())          # still works
        self.assertEqual(panel.door_opened, 1)
        run(client.async_close())


@unittest.skipIf(httpx is None, "httpx not installed")
class TestProfileInTheReport(unittest.TestCase):
    def test_report_states_the_three_verdicts(self):
        client = make_client(Panel())
        results, summary = run(client.async_probe())
        text = probe.format_probe_report(results, host="192.168.70.121", summary=summary)
        self.assertIn("нет — работаем опросом callStatus", text)
        self.assertIn("нет — кадры из RTSP-потока", text)
        self.assertIn("нет — кнопки меняют только состояние в HA", text)
        self.assertIn("авторизация", text)
        self.assertIn("DS-K1T341AM", text)
        run(client.async_close())

    def test_working_endpoints_are_marked_ok(self):
        client = make_client(Panel())
        results, _summary = run(client.async_probe())
        ok = {r.name for r in results if r.ok}
        self.assertIn("userCheck", ok)
        self.assertIn("deviceInfo", ok)
        self.assertIn("channel 101", ok)
        self.assertIn("channel 102", ok)
        self.assertIn("callStatus.json", ok)
        self.assertNotIn("alertStream", ok)
        self.assertNotIn("snapshot", ok)
        run(client.async_close())


if __name__ == "__main__":
    unittest.main()
