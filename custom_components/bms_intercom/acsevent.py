"""Access-control event log (AcsEvent): search request and readable records.

On DS-K1T341AM the call button may never start a call (no SIP server, no main
station configured), so callStatus stays "idle". The terminal's own event log
is the next place a button press can show up. This module builds the search
(`POST /ISAPI/AccessControl/AcsEvent?format=json`) and turns the answer into
lines a human can match against "I pressed the button at 15:31".

Pure module: no I/O, no Home Assistant — unit-testable on its own.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from .endpoints import redact

ACS_EVENT_PATH = "/ISAPI/AccessControl/AcsEvent?format=json"
PAGE_SIZE = 30
MAX_PAGES = 3

#: Hikvision major event families (ISAPI access-control spec).
MAJOR_NAMES = {
    1: "тревога",
    2: "исключение",
    3: "операция",
    5: "событие",
}

#: Fields printed first, in this order, when a record carries them.
PRIMARY_FIELDS = ("name", "cardNo", "currentVerifyMode")
#: Fields that are shown in their own columns or are pure noise.
_SKIP_EXTRA = {"time", "major", "minor", *PRIMARY_FIELDS}


def panel_now(time_doc: str | None) -> tuple[datetime, str]:
    """The panel's own clock from /ISAPI/System/time, else ours.

    Searching by the panel's clock avoids missing the press when the panel
    and Home Assistant disagree about the time or the time zone.
    """
    if time_doc:
        for tag in ("localTime",):
            start = time_doc.find(f"<{tag}>")
            end = time_doc.find(f"</{tag}>")
            if start >= 0 and end > start:
                raw = time_doc[start + len(tag) + 2 : end].strip()
                try:
                    return datetime.fromisoformat(raw), "часы панели"
                except ValueError:
                    break
        try:
            data = json.loads(time_doc)
            raw = data.get("Time", {}).get("localTime", "")
            if raw:
                return datetime.fromisoformat(raw), "часы панели"
        except (ValueError, AttributeError):
            pass
    return datetime.now().astimezone(), "часы Home Assistant"


def _stamp(moment: datetime) -> str:
    """ISAPI time format: 2026-09-23T15:31:07+05:00 (no microseconds)."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.replace(microsecond=0).isoformat()


def search_body(
    end: datetime,
    *,
    minutes: int = 15,
    position: int = 0,
    search_id: str | None = None,
) -> str:
    """JSON body for one page of the AcsEvent search."""
    start = end - timedelta(minutes=minutes)
    return json.dumps(
        {
            "AcsEventCond": {
                "searchID": search_id or uuid.uuid4().hex,
                "searchResultPosition": position,
                "maxResults": PAGE_SIZE,
                "major": 0,          # 0 = all families
                "minor": 0,          # 0 = all kinds
                "startTime": _stamp(start),
                "endTime": _stamp(end),
            }
        },
        ensure_ascii=False,
    )


def parse_page(text: str) -> tuple[list[dict[str, Any]], str, int]:
    """(records, responseStatusStrg, numOfMatches) from one search answer."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], "не JSON", 0
    block = data.get("AcsEvent", {}) if isinstance(data, dict) else {}
    records = block.get("InfoList") or []
    if not isinstance(records, list):
        records = []
    status = str(block.get("responseStatusStrg", ""))
    try:
        matches = int(block.get("numOfMatches", len(records)))
    except (TypeError, ValueError):
        matches = len(records)
    return [r for r in records if isinstance(r, dict)], status, matches


def _major_label(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    name = MAJOR_NAMES.get(number)
    return f"{number} ({name})" if name else str(number)


def _minor_label(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number} (0x{number:x})"


def format_record(record: dict[str, Any], *, secrets: tuple[str | None, ...] = ()) -> str:
    """One line per event: time, major, minor, then who/how, then the rest."""
    parts = [
        str(record.get("time", "—")),
        f"major={_major_label(record.get('major'))}",
        f"minor={_minor_label(record.get('minor'))}",
    ]
    for key in PRIMARY_FIELDS:
        value = record.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    extra = [
        f"{key}={value}"
        for key, value in record.items()
        if key not in _SKIP_EXTRA
        and value not in (None, "")
        and not isinstance(value, (dict, list))
    ]
    line = "  ".join(parts)
    if extra:
        rest = ", ".join(extra)
        if len(rest) > 240:
            rest = rest[:240] + "…"
        line += f"  | ещё: {rest}"
    return redact(line, *secrets)


def format_section(
    records: list[dict[str, Any]],
    *,
    window: str,
    status: str,
    secrets: tuple[str | None, ...] = (),
) -> list[str]:
    """Report lines for the event-log section, oldest first."""
    ordered = sorted(records, key=lambda r: str(r.get("time", "")))
    lines = [
        f"  журнал событий AcsEvent — {window} — {len(ordered)} зап., "
        f"ответ панели: {status or '—'}"
    ]
    if not ordered:
        lines.append("      (записей нет)")
    for record in ordered:
        lines.append("      " + format_record(record, secrets=secrets))
    return lines
