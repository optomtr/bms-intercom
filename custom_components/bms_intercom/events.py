"""Parsing of the Hikvision alertStream (ISAPI event notifications).

`GET /ISAPI/Event/notification/alertStream` is a never-ending multipart
response: the panel pushes one part per event, XML on most firmwares and JSON
on a few. Boundaries differ between models, so instead of trusting the
multipart framing we scan the byte stream for complete documents
(`<EventNotificationAlert>…</EventNotificationAlert>` or a balanced JSON
object). That survives odd boundaries, keep-alive padding and chunk splits in
the middle of a tag.

Pure module: no I/O, no Home Assistant — unit-testable on its own.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any

_LOGGER = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_ANSWERED = "answered"

_XML_ROOT = "EventNotificationAlert"
_XML_OPEN = re.compile(rf"<\s*{_XML_ROOT}\b")
_XML_CLOSE = f"</{_XML_ROOT}>"

# Keep-alive padding and huge bodies must not grow the buffer forever.
MAX_BUFFER = 256 * 1024

#: Event types that carry a call/doorbell for us.
CALL_EVENT_TYPES = {
    "videointercom",
    "video intercom",
    "doorbell",
    "callstatus",
    "callsignal",
    "callhelp",
}

#: Нажатие кнопки вызова на терминале доступа (DS-K1T341AM и родня) приходит
#: не как videoIntercom, а как AccessControllerEvent с числовыми кодами.
#: Коды — документированные Hikvision, раздел MAJOR_EVENT (0x5) таблицы
#: «Access Control Event Types» ISAPI / заголовка HCNetSDK.h:
#:   MINOR_DOORBELL_RINGING = 0x25 (37) — «Doorbell ring»,
#:   MINOR_CALL_CENTER      = 0x33 (51) — «Call center» (кнопка настроена
#:                                        звонить на центр управления).
#: https://tpp.hikvision.com/Wiki/ISAPI/Access%20Control%20on%20Person/GUID-079BE986-6D55-4F18-A4F2-A73DBD7442F9.html
#: https://github.com/Chise1/pyhk/blob/master/hcn_define.h (MAJOR_EVENT 0x5)
#: В alertStream ISAPI коды приходят десятичными числами (majorEventType=5).
ACS_EVENT_TYPE = "accesscontrollerevent"
ACS_MAJOR_EVENT = 0x5
ACS_CALL_MINORS = frozenset({0x25, 0x33})

#: Exact status words, lowercased.
_EXACT = {
    "ring": STATE_RINGING,
    "ringing": STATE_RINGING,
    "calling": STATE_RINGING,
    "call": STATE_RINGING,
    "bell": STATE_RINGING,
    "doorbell": STATE_RINGING,
    "oncall": STATE_ANSWERED,
    "on call": STATE_ANSWERED,
    "talking": STATE_ANSWERED,
    "answered": STATE_ANSWERED,
    "answer": STATE_ANSWERED,
    "idle": STATE_IDLE,
    "hangup": STATE_IDLE,
    "hang up": STATE_IDLE,
    "ended": STATE_IDLE,
    "end": STATE_IDLE,
    "endcall": STATE_IDLE,
    "reject": STATE_IDLE,
    "refuse": STATE_IDLE,
}

# Checked in this order so "hangup"/"endCall" win over the "call" substring.
_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("hangup", STATE_IDLE),
    ("hang_up", STATE_IDLE),
    ("endcall", STATE_IDLE),
    ("callend", STATE_IDLE),
    ("reject", STATE_IDLE),
    ("refuse", STATE_IDLE),
    ("idle", STATE_IDLE),
    ("oncall", STATE_ANSWERED),
    ("talking", STATE_ANSWERED),
    ("answer", STATE_ANSWERED),
    ("ring", STATE_RINGING),
    ("calling", STATE_RINGING),
    ("doorbell", STATE_RINGING),
    ("bell", STATE_RINGING),
)

#: Leaf tags that may hold the call status, most specific first.
_STATUS_FIELDS = (
    "callstatus",
    "status",
    "substatus",
    "subeventtype",
    "callevent",
    "eventdescription",
    "eventtype",
)


def _strip_ns(tag: str) -> str:
    return tag.rpartition("}")[2]


def _flatten_xml(elem: ET.Element, out: dict[str, str]) -> None:
    for child in elem:
        _flatten_xml(child, out)
        text = (child.text or "").strip()
        key = _strip_ns(child.tag).lower()
        if text and key not in out:
            out[key] = text


def _flatten_json(obj: Any, out: dict[str, str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                _flatten_json(value, out)
            elif value is not None:
                k = str(key).lower()
                if k not in out:
                    out[k] = str(value)
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json(item, out)


def parse_document(text: str) -> dict[str, Any] | None:
    """Turn one XML or JSON event document into a flat dict."""
    text = text.strip()
    if not text:
        return None
    fields: dict[str, str] = {}
    if text.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        _flatten_xml(root, fields)
        root_text = (root.text or "").strip()
        if root_text:
            fields.setdefault(_strip_ns(root.tag).lower(), root_text)
    else:
        try:
            fields_src = json.loads(text)
        except ValueError:
            return None
        _flatten_json(fields_src, fields)
    if not fields:
        return None
    return {
        "type": fields.get("eventtype", "").lower(),
        "state": fields.get("eventstate", "").lower(),
        "channel": fields.get("channelid") or fields.get("channelid", ""),
        "fields": fields,
        "raw": text,
    }


class AlertStreamParser:
    """Feed it raw stream bytes, get back parsed events."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: bytes | str) -> list[dict[str, Any]]:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self._buf += chunk
        events: list[dict[str, Any]] = []
        while True:
            doc, rest = self._take_document(self._buf)
            if doc is None:
                break
            self._buf = rest
            parsed = parse_document(doc)
            if parsed is not None:
                events.append(parsed)
        self._trim()
        return events

    def _trim(self) -> None:
        if len(self._buf) <= MAX_BUFFER:
            return
        # Keep the tail: a document start is somewhere near the end, if at all.
        self._buf = self._buf[-MAX_BUFFER // 2 :]

    @staticmethod
    def _take_document(buf: str) -> tuple[str | None, str]:
        """Cut the earliest complete XML or JSON document out of `buf`."""
        xml_match = _XML_OPEN.search(buf)
        xml_start = xml_match.start() if xml_match else -1
        json_start = buf.find("{")

        starts = [s for s in (xml_start, json_start) if s >= 0]
        if not starts:
            return None, buf
        first = min(starts)

        if first == xml_start:
            end = buf.find(_XML_CLOSE, xml_start)
            if end < 0:
                return None, buf
            end += len(_XML_CLOSE)
            return buf[xml_start:end], buf[end:]

        end = _match_braces(buf, json_start)
        if end < 0:
            return None, buf
        return buf[json_start:end], buf[end:]


def _match_braces(buf: str, start: int) -> int:
    """Index just past the object opened at `start`, or -1 if incomplete."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(buf)):
        ch = buf[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def is_call_event(event: dict[str, Any]) -> bool:
    """Does this event say anything about a call / doorbell press?"""
    if event.get("type", "") in CALL_EVENT_TYPES:
        return True
    fields = event.get("fields", {})
    if any(key.startswith("videointercom") for key in fields):
        return True
    return "callstatus" in fields


def _match_word(value: str) -> str | None:
    value = value.strip().lower()
    if not value:
        return None
    if value in _EXACT:
        return _EXACT[value]
    squashed = value.replace(" ", "").replace("-", "").replace("_", "")
    for needle, state in _SUBSTRINGS:
        if needle.replace("_", "") in squashed:
            return state
    return None


def _int_field(fields: dict[str, str], key: str) -> int | None:
    try:
        return int(fields.get(key, "").strip())
    except ValueError:
        return None


def is_acs_call(event: dict[str, Any]) -> bool:
    """Кнопка вызова на терминале доступа (см. ACS_CALL_MINORS)."""
    if event.get("type", "") != ACS_EVENT_TYPE:
        return False
    fields = event.get("fields", {})
    return (
        _int_field(fields, "majoreventtype") == ACS_MAJOR_EVENT
        and _int_field(fields, "subeventtype") in ACS_CALL_MINORS
    )


def call_state_from_event(event: dict[str, Any] | None) -> str | None:
    """Map one parsed event to idle/ringing/answered, or None if unrelated."""
    if event and is_acs_call(event):
        # Разовое событие: «конца звонка» терминал не присылает, сброс делают
        # окно звонка и таймер вызова (callsource).
        return STATE_RINGING
    if not event or not is_call_event(event):
        return None
    fields: dict[str, str] = event.get("fields", {})
    for key in _STATUS_FIELDS:
        value = fields.get(key)
        if not value:
            continue
        state = _match_word(value)
        if state is not None:
            return state
    # No status word: fall back to the event lifecycle flag. A doorbell press
    # arrives as active/inactive with no further detail.
    state_flag = event.get("state", "")
    if state_flag in ("inactive", "0", "false"):
        return STATE_IDLE
    if state_flag == "active":
        return STATE_RINGING
    return None


# --- диагностика: нераспознанные события панели ------------------------------
#: Поля, в названии которых есть эти куски, не пишем ни в журнал, ни в атрибут:
#: персональные данные (имя, табельный номер, карта, телефон, пароль) и тяжёлое
#: (ссылки на фото лица). Атрибут видит любой пользователь HA и он уходит
#: в интерфейс — персональное туда попадать не должно.
_PRIVATE_KEY_PARTS = (
    "name", "employee", "card", "person", "picture", "pic", "url", "face",
    "phone", "pwd", "password",
)
#: Длинные значения (base64, XML-обрывки) — это шум, а не код события.
MAX_FIELD_LEN = 80
#: Тип и state уже показаны отдельно — не дублируем их в полях.
_SHOWN_APART = frozenset({"eventtype", "eventstate"})
PANEL_EVENTS_KEEP = 10
#: Одна и та же сигнатура пишется в журнал на INFO не чаще раза в 10 минут:
#: терминал шлёт по несколько событий на каждое лицо/карту/нажатие.
PANEL_EVENT_INFO_EVERY = 600.0


def is_heartbeat(event: dict[str, Any]) -> bool:
    """Пульс alertStream (videoloss/inactive раз в несколько секунд).

    Если его хранить, он за минуту вытеснит из десяти мест всё полезное.
    """
    return event.get("type") == "videoloss" and event.get("state") == "inactive"


def panel_event_fields(event: dict[str, Any]) -> dict[str, str]:
    """Короткие неперсональные поля события — то, по чему видно его код."""
    out: dict[str, str] = {}
    for key, value in event.get("fields", {}).items():
        if key in _SHOWN_APART or any(p in key for p in _PRIVATE_KEY_PARTS):
            continue
        value = str(value).strip()
        if value and len(value) <= MAX_FIELD_LEN:
            out[key] = value
    return out


def panel_event_signature(event: dict[str, Any]) -> str:
    fields = event.get("fields", {})
    return "/".join((
        event.get("type", ""),
        fields.get("majoreventtype", ""),
        fields.get("subeventtype") or fields.get("eventtype", ""),
    ))


class PanelEventLog:
    """Последние нераспознанные события панели — для атрибута panel_events.

    Зачем: на объекте терминал на нажатие «Вызов» шлёт события, которые мы
    не узнаём, и без их полей не понять, что распознавать. Журнал HA владелец
    не читает — атрибут видно прямо в карточке сущности.
    """

    def __init__(
        self,
        keep: int = PANEL_EVENTS_KEEP,
        info_every: float = PANEL_EVENT_INFO_EVERY,
    ) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=keep)
        self._info_every = info_every
        self._last_info: dict[str, float] = {}

    def add(
        self, event: dict[str, Any], now: float, stamp: str
    ) -> tuple[str, bool] | None:
        """Запомнить событие -> (строка для журнала, писать ли на INFO).

        None — пульс потока, ничего не запомнено.
        """
        if is_heartbeat(event):
            return None
        item = {
            "time": stamp,
            "type": event.get("type", ""),
            "state": event.get("state", ""),
            "fields": panel_event_fields(event),
        }
        self._items.append(item)
        sig = panel_event_signature(event)
        last = self._last_info.get(sig)
        loud = last is None or now - last >= self._info_every
        if loud:
            self._last_info[sig] = now
            if len(self._last_info) > 256:  # сигнатуры не должны копиться вечно
                self._last_info = {
                    k: t for k, t in self._last_info.items()
                    if now - t < self._info_every
                }
        line = " ".join(
            [f"type={item['type']}", f"state={item['state']}"]
            + [f"{k}={v}" for k, v in item["fields"].items()]
        )
        return line, loud

    @property
    def items(self) -> list[dict[str, Any]]:
        """Копия, по порядку прихода: самое свежее — последним."""
        return [{**i, "fields": dict(i["fields"])} for i in self._items]
