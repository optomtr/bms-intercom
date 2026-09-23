"""alertStream parsing: a captured multipart sample of a videoIntercom call.

The sample below is written by hand in the shape Hikvision firmwares send on
`GET /ISAPI/Event/notification/alertStream`: one multipart part per event,
XML body, boundary lines in between.

Run: python3 -m unittest discover -s tests -v   (no pytest, no Home Assistant)
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
