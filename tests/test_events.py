"""alertStream parsing: a captured multipart sample of a videoIntercom call.

The sample below is written by hand in the shape Hikvision firmwares send on
`GET /ISAPI/Event/notification/alertStream`: one multipart part per event,
XML body, boundary lines in between.

Run: python3 -m unittest discover -s tests -v   (no pytest, no Home Assistant)
"""
from __future__ import annotations

import json
import unittest

from _loader import load

events = load("events")


CALL_START = (
    "--boundary\r\n"
    "Content-Type: application/xml; charset=\"UTF-8\"\r\n"
    "Content-Length: 476\r\n"
    "\r\n"
    "<EventNotificationAlert version=\"2.0\" "
    "xmlns=\"http://www.isapi.org/ver20/XMLSchema\">\r\n"
    "<ipAddress>192.168.70.121</ipAddress>\r\n"
    "<portNo>80</portNo>\r\n"
    "<protocol>HTTP</protocol>\r\n"
    "<macAddress>a4:14:37:11:22:33</macAddress>\r\n"
    "<channelID>1</channelID>\r\n"
    "<dateTime>2026-09-23T11:20:01+05:00</dateTime>\r\n"
    "<activePostCount>1</activePostCount>\r\n"
    "<eventType>videoIntercom</eventType>\r\n"
    "<eventState>active</eventState>\r\n"
    "<eventDescription>videoIntercom Event</eventDescription>\r\n"
    "<VideoIntercom>\r\n"
    "<status>ring</status>\r\n"
    "<callNumber>1</callNumber>\r\n"
    "</VideoIntercom>\r\n"
    "</EventNotificationAlert>\r\n"
)

CALL_END = (
    "--boundary\r\n"
    "Content-Type: application/xml; charset=\"UTF-8\"\r\n"
    "Content-Length: 462\r\n"
    "\r\n"
    "<EventNotificationAlert version=\"2.0\" "
    "xmlns=\"http://www.isapi.org/ver20/XMLSchema\">\r\n"
    "<ipAddress>192.168.70.121</ipAddress>\r\n"
    "<channelID>1</channelID>\r\n"
    "<dateTime>2026-09-23T11:20:37+05:00</dateTime>\r\n"
    "<activePostCount>0</activePostCount>\r\n"
    "<eventType>videoIntercom</eventType>\r\n"
    "<eventState>inactive</eventState>\r\n"
    "<eventDescription>videoIntercom Event</eventDescription>\r\n"
    "<VideoIntercom>\r\n"
    "<status>hangUp</status>\r\n"
    "</VideoIntercom>\r\n"
    "</EventNotificationAlert>\r\n"
    "--boundary--\r\n"
)

DOORBELL_JSON = (
    "--boundary\r\n"
    "Content-Type: application/json\r\n"
    "\r\n"
    '{"ipAddress":"192.168.70.121","channelID":1,"eventType":"doorbell",'
    '"eventState":"active","eventDescription":"doorbell","Doorbell":'
    '{"status":"ring"}}\r\n'
)

CARD_SWIPE = (
    "--boundary\r\n"
    "Content-Type: application/xml\r\n"
    "\r\n"
    "<EventNotificationAlert version=\"2.0\">\r\n"
    "<eventType>AccessControllerEvent</eventType>\r\n"
    "<eventState>active</eventState>\r\n"
    "<AccessControllerEvent>\r\n"
    "<subEventType>75</subEventType>\r\n"
    "<cardNo>0012345678</cardNo>\r\n"
    "</AccessControllerEvent>\r\n"
    "</EventNotificationAlert>\r\n"
)


class TestAlertStreamParsing(unittest.TestCase):
    def test_call_start_and_end_in_one_chunk(self):
        parser = events.AlertStreamParser()
        parsed = parser.feed((CALL_START + CALL_END).encode())
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["type"], "videointercom")
        self.assertEqual(
            events.call_state_from_event(parsed[0]), events.STATE_RINGING
        )
        self.assertEqual(
            events.call_state_from_event(parsed[1]), events.STATE_IDLE
        )

    def test_events_split_across_chunks_are_reassembled(self):
        stream = (CALL_START + CALL_END).encode()
        parser = events.AlertStreamParser()
        collected = []
        for start in range(0, len(stream), 37):  # split mid-tag on purpose
            collected.extend(parser.feed(stream[start:start + 37]))
        self.assertEqual(
            [events.call_state_from_event(e) for e in collected],
            [events.STATE_RINGING, events.STATE_IDLE],
        )

    def test_nothing_is_emitted_until_the_document_is_complete(self):
        parser = events.AlertStreamParser()
        half = CALL_START[: len(CALL_START) // 2].encode()
        self.assertEqual(parser.feed(half), [])
        rest = CALL_START[len(CALL_START) // 2 :].encode()
        self.assertEqual(len(parser.feed(rest)), 1)

    def test_json_doorbell_event(self):
        parser = events.AlertStreamParser()
        parsed = parser.feed(DOORBELL_JSON.encode())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(
            events.call_state_from_event(parsed[0]), events.STATE_RINGING
        )

    def test_card_swipe_is_not_a_call(self):
        parser = events.AlertStreamParser()
        parsed = parser.feed(CARD_SWIPE.encode())
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(events.call_state_from_event(parsed[0]))

    def test_keepalive_noise_does_not_break_the_parser(self):
        parser = events.AlertStreamParser()
        self.assertEqual(parser.feed(b"\r\n--boundary\r\n"), [])
        parsed = parser.feed(CALL_START.encode())
        self.assertEqual(len(parsed), 1)

    def test_answered_state_is_recognised(self):
        parser = events.AlertStreamParser()
        doc = CALL_START.replace("<status>ring</status>", "<status>onCall</status>")
        parsed = parser.feed(doc.encode())
        self.assertEqual(
            events.call_state_from_event(parsed[0]), events.STATE_ANSWERED
        )

    def test_call_status_poll_document_maps_too(self):
        """The poll fallback reuses the same word map."""
        doc = events.parse_document('{"CallStatus":{"status":"calling"}}')
        doc["type"] = "callstatus"
        self.assertEqual(events.call_state_from_event(doc), events.STATE_RINGING)

    def test_inactive_without_a_status_word_means_idle(self):
        doc = events.parse_document(
            "<EventNotificationAlert><eventType>videoIntercom</eventType>"
            "<eventState>inactive</eventState></EventNotificationAlert>"
        )
        self.assertEqual(events.call_state_from_event(doc), events.STATE_IDLE)

    def test_garbage_is_ignored(self):
        parser = events.AlertStreamParser()
        self.assertEqual(parser.feed(b"<EventNotificationAlert>broken"), [])
        self.assertEqual(parser.feed(b"not xml at all\r\n"), [])



def acs_event(major: int, sub: int, **extra) -> dict:
    """AccessControllerEvent в форме ISAPI JSON (с персональными полями)."""
    body = {
        "deviceName": "Access Controller",
        "majorEventType": major,
        "subEventType": sub,
        "name": "Иванов Иван",
        "employeeNoString": "1007",
        "cardNo": "0012345678",
        "pictureURL": "http://192.168.70.121/LOCALS/pic/acsLinkCap/1.jpg@WEB0",
        "cardReaderNo": 1,
        "serialNo": 4242,
        **extra,
    }
    doc = events.parse_document(json.dumps({
        "ipAddress": "192.168.70.121",
        "dateTime": "2026-09-24T10:00:00+05:00",
        "eventType": "AccessControllerEvent",
        "eventState": "active",
        "AccessControllerEvent": body,
    }))
    assert doc is not None
    return doc


PRIVATE_KEYS = ("name", "employeenostring", "cardno", "pictureurl",
                "devicename", "cardreaderno")


class TestAccessControllerCall(unittest.TestCase):
    """Кнопка «Вызов» терминала: MAJOR_EVENT 0x5 + 0x25/0x33 (HCNetSDK/ISAPI)."""

    def test_doorbell_ringing_code_rings(self):
        self.assertEqual(
            events.call_state_from_event(acs_event(5, 0x25)), events.STATE_RINGING
        )

    def test_call_center_code_rings(self):
        self.assertEqual(
            events.call_state_from_event(acs_event(5, 0x33)), events.STATE_RINGING
        )

    def test_other_codes_are_not_a_call(self):
        # 5/75 — лицо распознано; 3/0x25 — тот же минор, но другой major.
        self.assertIsNone(events.call_state_from_event(acs_event(5, 75)))
        self.assertIsNone(events.call_state_from_event(acs_event(3, 0x25)))


class TestPanelEventLog(unittest.TestCase):
    def test_unrecognised_event_is_kept_without_personal_fields(self):
        event = acs_event(5, 75)
        self.assertIsNone(events.call_state_from_event(event))
        log = events.PanelEventLog()
        line, loud = log.add(event, 0.0, "2026-09-24T10:00:00+05:00")
        self.assertTrue(loud)
        [item] = log.items
        self.assertEqual(item["type"], "accesscontrollerevent")
        self.assertEqual(item["state"], "active")
        self.assertEqual(item["time"], "2026-09-24T10:00:00+05:00")
        fields = item["fields"]
        self.assertEqual(fields["majoreventtype"], "5")
        self.assertEqual(fields["subeventtype"], "75")
        self.assertEqual(fields["serialno"], "4242")
        self.assertEqual(fields["ipaddress"], "192.168.70.121")  # IP можно
        for key in PRIVATE_KEYS:
            self.assertNotIn(key, fields)
        for secret in ("Иванов", "1007", "0012345678", "acsLinkCap"):
            self.assertNotIn(secret, line)
            self.assertNotIn(secret, str(log.items))
        self.assertIn("majoreventtype=5", line)
        self.assertIn("subeventtype=75", line)

    def test_long_values_are_dropped(self):
        log = events.PanelEventLog()
        log.add(acs_event(5, 75, blob="x" * 81, short="y" * 80), 0.0, "t")
        fields = log.items[0]["fields"]
        self.assertNotIn("blob", fields)
        self.assertEqual(fields["short"], "y" * 80)

    def test_only_the_last_ten_are_kept(self):
        log = events.PanelEventLog()
        for n in range(15):
            log.add(acs_event(5, 100 + n), float(n), f"t{n}")
        items = log.items
        self.assertEqual(len(items), 10)
        self.assertEqual(items[0]["time"], "t5")
        self.assertEqual(items[-1]["fields"]["subeventtype"], "114")

    def test_info_once_per_signature_per_ten_minutes(self):
        log = events.PanelEventLog()
        self.assertTrue(log.add(acs_event(5, 75), 0.0, "a")[1])
        self.assertFalse(log.add(acs_event(5, 75), 1.0, "b")[1])
        self.assertTrue(log.add(acs_event(5, 76), 2.0, "c")[1])   # другая сигнатура
        self.assertFalse(log.add(acs_event(5, 75), 599.0, "d")[1])
        self.assertTrue(log.add(acs_event(5, 75), 600.0, "e")[1])
        self.assertEqual(len(log.items), 5)  # в атрибут идут все, тише — только журнал

    def test_stream_heartbeat_is_not_kept(self):
        log = events.PanelEventLog()
        beat = events.parse_document(
            "<EventNotificationAlert><eventType>videoloss</eventType>"
            "<eventState>inactive</eventState></EventNotificationAlert>"
        )
        self.assertIsNone(log.add(beat, 0.0, "t"))
        self.assertEqual(log.items, [])


if __name__ == "__main__":
    unittest.main()
