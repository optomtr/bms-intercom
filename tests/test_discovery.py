"""Discovery probes: event log (AcsEvent), push targets (httpHosts), caps.

Goal on DS-K1T341AM: find where "someone pressed the call button" is visible
when callStatus never leaves "idle" (no SIP server / main station set).

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from _loader import load

acs = load("acsevent")

TZ5 = timezone(timedelta(hours=5))
PANEL_TIME = (
    '<?xml version="1.0"?><Time><timeMode>NTP</timeMode>'
    "<localTime>2026-09-23T15:35:00+05:00</localTime>"
    "<timeZone>CST-5:00:00</timeZone></Time>"
)


class TestSearchBody(unittest.TestCase):
    def test_shape_matches_the_isapi_search(self):
        end = datetime(2026, 9, 23, 15, 35, 0, 123456, tzinfo=TZ5)
        body = json.loads(acs.search_body(end, minutes=15, search_id="abc"))
        cond = body["AcsEventCond"]
        self.assertEqual(cond["searchID"], "abc")
        self.assertEqual(cond["searchResultPosition"], 0)
        self.assertEqual(cond["maxResults"], 30)
        self.assertEqual((cond["major"], cond["minor"]), (0, 0))
        self.assertEqual(cond["startTime"], "2026-09-23T15:20:00+05:00")
        self.assertEqual(cond["endTime"], "2026-09-23T15:35:00+05:00")

    def test_pagination_position_is_carried(self):
        end = datetime(2026, 9, 23, 15, 35, tzinfo=TZ5)
        body = json.loads(acs.search_body(end, position=30, search_id="abc"))
        self.assertEqual(body["AcsEventCond"]["searchResultPosition"], 30)

    def test_search_uses_the_panels_clock(self):
        moment, clock = acs.panel_now(PANEL_TIME)
        self.assertEqual(clock, "часы панели")
        self.assertEqual(moment.isoformat(), "2026-09-23T15:35:00+05:00")

    def test_falls_back_to_our_clock(self):
        moment, clock = acs.panel_now("")
        self.assertEqual(clock, "часы Home Assistant")
        self.assertIsNotNone(moment.tzinfo)

    def test_json_time_document(self):
        moment, clock = acs.panel_now(
            '{"Time": {"localTime": "2026-09-23T15:35:00+05:00"}}'
        )
        self.assertEqual(clock, "часы панели")
        self.assertEqual(moment.hour, 15)


class TestRecords(unittest.TestCase):
    RECORD = {
        "major": 5, "minor": 75, "time": "2026-09-23T15:31:07+05:00",
        "cardNo": "0012345678", "name": "Иван", "currentVerifyMode": "cardOrFace",
        "doorNo": 1, "employeeNoString": "7", "serialNo": 120,
    }

    def test_primary_fields_come_first(self):
        line = acs.format_record(self.RECORD)
        self.assertTrue(line.startswith("2026-09-23T15:31:07+05:00"))
        self.assertIn("major=5 (событие)", line)
        self.assertIn("minor=75 (0x4b)", line)
        self.assertIn("name=Иван", line)
        self.assertIn("cardNo=0012345678", line)
        self.assertIn("currentVerifyMode=cardOrFace", line)
        self.assertIn("ещё: doorNo=1, employeeNoString=7, serialNo=120", line)

    def test_unknown_major_is_shown_raw(self):
        line = acs.format_record({"major": 9, "minor": 1, "time": "t"})
        self.assertIn("major=9 ", line)

    def test_parse_page_reads_infolist(self):
        page = json.dumps({"AcsEvent": {
            "searchID": "x", "responseStatusStrg": "MORE",
            "numOfMatches": 1, "totalMatches": 31, "InfoList": [self.RECORD],
        }})
        records, status, matches = acs.parse_page(page)
        self.assertEqual(len(records), 1)
        self.assertEqual(status, "MORE")
        self.assertEqual(matches, 1)

    def test_parse_page_tolerates_garbage(self):
        self.assertEqual(acs.parse_page("not json"), ([], "не JSON", 0))
        self.assertEqual(acs.parse_page('{"AcsEvent": {}}')[0], [])

    def test_section_orders_oldest_first_and_says_when_empty(self):
        later = dict(self.RECORD, time="2026-09-23T15:33:00+05:00")
        lines = acs.format_section([later, self.RECORD], window="w", status="OK")
        self.assertIn("2 зап.", lines[0])
        self.assertLess(
            "\n".join(lines).index("15:31:07"), "\n".join(lines).index("15:33:00")
        )
        empty = acs.format_section([], window="w", status="NO MATCH")
        self.assertIn("(записей нет)", "\n".join(empty))


@unittest.skipIf(httpx is None, "httpx not installed")
class TestDiscoveryProbe(unittest.TestCase):
    PASSWORD_ON_PANEL = "PushTarget$ecret"

    def _panel(self, pages):
        from test_ds_k1t341am import Panel

        seen_bodies: list[dict] = []
        test = self

        class DiscoveryPanel(Panel):
            def __call__(self, request):
                path = request.url.path
                if path == "/ISAPI/System/time":
                    return httpx.Response(
                        200, headers={"Content-Type": "application/xml"}, text=PANEL_TIME
                    )
                if path == "/ISAPI/AccessControl/AcsEvent" and request.method == "POST":
                    body = json.loads(request.content)
                    seen_bodies.append(body)
                    return httpx.Response(
                        200, headers={"Content-Type": "application/json"},
                        text=json.dumps(pages[min(len(seen_bodies), len(pages)) - 1]),
                    )
                if path == "/ISAPI/AccessControl/capabilities":
                    return httpx.Response(
                        200, headers={"Content-Type": "application/xml"},
                        text="<AccessControl><isSupportAcsEvent>true</isSupportAcsEvent>"
                             "<isSupportRemoteControlDoor>true</isSupportRemoteControlDoor>"
                             "</AccessControl>",
                    )
                if path == "/ISAPI/Event/notification/httpHosts":
                    return httpx.Response(
                        200, headers={"Content-Type": "application/xml"},
                        text="<HttpHostNotificationList><HttpHostNotification><id>1</id>"
                             "<url>/api/webhook/x</url><protocolType>HTTP</protocolType>"
                             "<ipAddress>0.0.0.0</ipAddress><portNo>80</portNo>"
                             "<userName>ha</userName>"
                             f"<password>{test.PASSWORD_ON_PANEL}</password>"
                             "</HttpHostNotification></HttpHostNotificationList>",
                    )
                return super().__call__(request)

        return DiscoveryPanel(), seen_bodies

    def _probe(self, pages):
        isapi = load("isapi")
        probe = load("probe")
        panel, bodies = self._panel(pages)
        client = isapi.ISAPIClient("192.168.70.121", "admin", "Sekret123!")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(panel))

        async def run():
            results, summary = await client.async_probe(acs_minutes=15)
            await client.async_close()
            return results, summary

        results, summary = asyncio.run(run())
        text = probe.format_probe_report(
            results, host="192.168.70.121", summary=summary,
            sections=client.probe_sections,
        )
        return client, results, text, bodies

    PRESS = {"major": 5, "minor": 75, "time": "2026-09-23T15:31:07+05:00",
             "currentVerifyMode": "invalid"}

    def test_button_press_record_lands_in_the_report(self):
        page = {"AcsEvent": {"responseStatusStrg": "OK", "numOfMatches": 1,
                             "InfoList": [self.PRESS]}}
        client, _results, text, bodies = self._probe([page])
        self.assertIn("журнал событий AcsEvent", text)
        self.assertIn("последние 15 мин (часы панели)", text)
        self.assertIn("2026-09-23T15:31:07+05:00  major=5 (событие)  minor=75 (0x4b)", text)
        self.assertEqual(client.probe_acs_events[0]["minor"], 75)
        # The window is the PANEL's last 15 minutes.
        cond = bodies[0]["AcsEventCond"]
        self.assertEqual(cond["endTime"], "2026-09-23T15:35:00+05:00")
        self.assertEqual(cond["startTime"], "2026-09-23T15:20:00+05:00")

    def test_more_pages_are_followed(self):
        first = {"AcsEvent": {"responseStatusStrg": "MORE", "numOfMatches": 1,
                              "InfoList": [self.PRESS]}}
        second = {"AcsEvent": {"responseStatusStrg": "OK", "numOfMatches": 1,
                               "InfoList": [dict(self.PRESS, time="2026-09-23T15:32:00+05:00")]}}
        client, _results, _text, bodies = self._probe([first, second])
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1]["AcsEventCond"]["searchResultPosition"], 1)
        self.assertEqual(
            bodies[0]["AcsEventCond"]["searchID"], bodies[1]["AcsEventCond"]["searchID"]
        )
        self.assertEqual(len(client.probe_acs_events), 2)

    def test_discovery_documents_are_shown_in_full_and_redacted(self):
        page = {"AcsEvent": {"responseStatusStrg": "NO MATCH", "numOfMatches": 0}}
        _client, results, text, _bodies = self._probe([page])
        names = {r.name for r in results}
        for name in ("systemTime", "accessControl.capabilities", "acsEvent.capabilities",
                     "httpHosts", "httpHosts.capabilities", "acsEvent.search p1"):
            self.assertIn(name, names)
        self.assertIn("<protocolType>HTTP</protocolType>", text)   # beyond 120 chars
        self.assertIn("isSupportRemoteControlDoor", text)
        self.assertNotIn(self.PASSWORD_ON_PANEL, text)             # never leaked
        self.assertIn("<password>***</password>", text)
        self.assertIn("(записей нет)", text)

    def test_catalogue_shape_is_kept(self):
        page = {"AcsEvent": {"responseStatusStrg": "OK", "numOfMatches": 0}}
        _client, _results, text, _bodies = self._probe([page])
        for marker in ("BMS Intercom — проверка панели", "авторизация:",
                       "проверка /ISAPI/Security/userCheck:", "userCheck: GET",
                       "callStatus.json: GET", "alertStream: GET", "реле двери:"):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
