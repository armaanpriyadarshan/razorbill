"""Calendar awareness via an iCal (ICS) feed.

`calendar_ics_url` points at a read-only calendar feed (Google Calendar:
Settings, "Secret address in iCal format"; Outlook: "Publish calendar").
When a recording starts, the daemon resolves which event is happening and
gives its title, attendees, and description to the copilot, `ask`, note
generation, and background-document selection.

The parser covers what real meeting calendars contain: timed VEVENTs with
TZID/UTC/floating times, folded lines, ATTENDEE/ORGANIZER, and the common
recurrence patterns (FREQ=DAILY/WEEKLY/MONTHLY with INTERVAL, BYDAY, UNTIL,
and EXDATE). It is not a full RFC 5545 implementation: COUNT-bounded rules,
BYSETPOS, and moved single instances of a series (RECURRENCE-ID) are
ignored, and all-day events are skipped because they are not meetings.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("razorbill")

EVENT_FILE = "event.json"
UTC = dt.timezone.utc
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def fetch(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "razorbill"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# --- parsing ---------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value: str, params: dict[str, str]) -> dt.datetime | None:
    """A timed ICS timestamp as an aware datetime; None for all-day dates."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return None
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None
    naive = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        return naive.replace(tzinfo=UTC)
    tzid = params.get("TZID", "")
    try:
        return naive.replace(tzinfo=ZoneInfo(tzid) if tzid else None).astimezone(UTC) \
            if tzid else naive.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    except (KeyError, ValueError):  # unknown TZID: treat as local
        return naive.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)


def _prop(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *raw_params = head.split(";")
    params = {}
    for p in raw_params:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name.upper(), params, value


def parse(text: str) -> list[dict]:
    """VEVENTs as dicts: summary, description, location, organizer,
    attendees, start/end (aware datetimes), rrule (dict), exdates (set)."""
    events: list[dict] = []
    ev: dict | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {"attendees": [], "exdates": set(), "rrule": {}}
            continue
        if line == "END:VEVENT":
            if ev and ev.get("start") and ev.get("end"):
                events.append(ev)
            ev = None
            continue
        if ev is None or ":" not in line:
            continue
        name, params, value = _prop(line)
        if name == "SUMMARY":
            ev["summary"] = _unescape(value)
        elif name == "DESCRIPTION":
            ev["description"] = _unescape(value)
        elif name == "LOCATION":
            ev["location"] = _unescape(value)
        elif name == "DTSTART":
            ev["start"] = _parse_dt(value, params)
        elif name == "DTEND":
            ev["end"] = _parse_dt(value, params)
        elif name == "ORGANIZER":
            ev["organizer"] = params.get("CN") or value.removeprefix("mailto:")
        elif name == "ATTENDEE":
            who = params.get("CN") or value.removeprefix("mailto:")
            if who and who not in ev["attendees"]:
                ev["attendees"].append(who)
        elif name == "RRULE":
            for part in value.split(";"):
                k, _, v = part.partition("=")
                ev["rrule"][k.upper()] = v
        elif name == "EXDATE":
            for chunk in value.split(","):
                exd = _parse_dt(chunk, params)
                if exd:
                    ev["exdates"].add(exd)
    return events


# --- occurrence resolution ----------------------------------------------------

def _occurrence_start(ev: dict, now: dt.datetime) -> dt.datetime | None:
    """The start of the occurrence containing (or nearest before) `now`,
    or None if the rule cannot put one on today's date."""
    start: dt.datetime = ev["start"]
    rule = ev["rrule"]
    if not rule:
        return start
    freq = rule.get("FREQ", "")
    interval = int(rule.get("INTERVAL", 1) or 1)
    until = _parse_dt(rule["UNTIL"], {}) if "UNTIL" in rule else None

    local = start.astimezone()  # recurrence arithmetic in local wall time
    today = now.astimezone().date()
    candidate = dt.datetime.combine(today, local.timetz())

    if candidate < local:
        return None
    if until and candidate > until.astimezone():
        return None

    days = (today - local.date()).days
    if freq == "DAILY":
        ok = days % interval == 0
        if "BYDAY" in rule:
            ok = ok and today.weekday() in {WEEKDAYS[d[-2:]] for d in rule["BYDAY"].split(",")}
    elif freq == "WEEKLY":
        bydays = {WEEKDAYS[d[-2:]] for d in rule["BYDAY"].split(",")} if "BYDAY" in rule \
            else {local.weekday()}
        weeks = (today - (local.date() - dt.timedelta(days=local.weekday()))).days // 7
        ok = today.weekday() in bydays and weeks % interval == 0
    elif freq == "MONTHLY":
        months = (today.year - local.year) * 12 + today.month - local.month
        ok = today.day == local.day and months % interval == 0
    elif freq == "YEARLY":
        ok = (today.month, today.day) == (local.month, local.day)
    else:
        return None
    if not ok:
        return None
    occurrence = candidate.astimezone(UTC)
    return None if occurrence in ev["exdates"] else occurrence


def current(events: list[dict], now: dt.datetime | None = None,
            early_minutes: float = 10.0) -> dict | None:
    """The event happening at `now` (joining a few minutes early counts).
    Overlaps resolve to the latest-starting event: the one being joined."""
    now = now or dt.datetime.now(UTC)
    best: tuple[dt.datetime, dict] | None = None
    for ev in events:
        occ = _occurrence_start(ev, now)
        if occ is None:
            continue
        duration = ev["end"] - ev["start"]
        if occ - dt.timedelta(minutes=early_minutes) <= now <= occ + duration:
            if best is None or occ > best[0]:
                best = (occ, ev)
    if best is None:
        return None
    occ, ev = best
    return {
        "title": ev.get("summary", ""),
        "description": (ev.get("description", "") or "")[:2000],
        "location": ev.get("location", ""),
        "organizer": ev.get("organizer", ""),
        "attendees": ev["attendees"][:30],
        "start": occ.isoformat(),
    }


# --- meeting-dir plumbing ----------------------------------------------------

def write_event(d: Path, event: dict) -> None:
    (d / EVENT_FILE).write_text(json.dumps(event))


def read_event(d: Path) -> dict:
    try:
        return json.loads((d / EVENT_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def describe(event: dict) -> str:
    """One block of text for prompts; empty for an empty event."""
    if not event:
        return ""
    parts = [f"Title: {event['title']}"] if event.get("title") else []
    if event.get("organizer"):
        parts.append(f"Organizer: {event['organizer']}")
    if event.get("attendees"):
        parts.append("Attendees: " + ", ".join(event["attendees"]))
    if event.get("location"):
        parts.append(f"Location: {event['location']}")
    if event.get("description"):
        parts.append(f"Description: {event['description']}")
    return "\n".join(parts)
