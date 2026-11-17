"""تقویم جلسات و صف پیش‌نویس‌ها — در فایل‌های JSON داخل پوشهٔ data/."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
MEETINGS = DATA / "meetings.json"
DRAFTS = DATA / "drafts.json"
_lock = threading.Lock()


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TIMEZONE", "Asia/Tehran"))


def now() -> datetime:
    return datetime.now(tz()).replace(second=0, microsecond=0)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, items: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_dt(value: str) -> datetime:
    """'2026-11-20T14:30' یا '2026-11-20 14:30' → زمان محلی آگاه از منطقهٔ زمانی."""
    dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    return dt.replace(tzinfo=tz()) if dt.tzinfo is None else dt.astimezone(tz())


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


# ---------- جلسات ----------

def list_meetings(start: str | None = None, end: str | None = None, include_cancelled: bool = False) -> list[dict]:
    items = _load(MEETINGS)
    s = parse_dt(start) if start else None
    e = parse_dt(end) if end else None
    out = []
    for m in items:
        if m.get("status") == "cancelled" and not include_cancelled:
            continue
        ms, me = parse_dt(m["start"]), parse_dt(m["end"])
        if s and me <= s:
            continue
        if e and ms >= e:
            continue
        out.append(m)
    return sorted(out, key=lambda m: m["start"])


def get_meeting(mid: str) -> dict:
    for m in _load(MEETINGS):
        if m["id"] == mid:
            return m
    raise KeyError(f"جلسهٔ {mid} پیدا نشد")


def conflicts(start: datetime, end: datetime, ignore_id: str | None = None) -> list[dict]:
    return [m for m in list_meetings()
            if m["id"] != ignore_id and parse_dt(m["start"]) < end and parse_dt(m["end"]) > start]


def add_meeting(title: str, start: str, duration_minutes: int, attendees: list[str],
                location: str = "", description: str = "") -> dict:
    s = parse_dt(start)
    m = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "start": fmt(s),
        "end": fmt(s + timedelta(minutes=duration_minutes)),
        "attendees": attendees,
        "location": location,
        "description": description,
        "status": "confirmed",
        "sequence": 0,
        "created": fmt(now()),
    }
    with _lock:
        items = _load(MEETINGS)
        items.append(m)
        _save(MEETINGS, items)
    return m


def update_meeting(mid: str, **changes) -> dict:
    with _lock:
        items = _load(MEETINGS)
        for m in items:
            if m["id"] != mid:
                continue
            dur = parse_dt(m["end"]) - parse_dt(m["start"])
            if changes.get("start"):
                s = parse_dt(changes.pop("start"))
                m["start"] = fmt(s)
                m["end"] = fmt(s + dur)
            if changes.get("duration_minutes"):
                m["end"] = fmt(parse_dt(m["start"]) + timedelta(minutes=int(changes.pop("duration_minutes"))))
            for k in ("title", "location", "description", "attendees", "status"):
                if changes.get(k) is not None:
                    m[k] = changes[k]
            m["sequence"] = m.get("sequence", 0) + 1
            _save(MEETINGS, items)
            return m
    raise KeyError(f"جلسهٔ {mid} پیدا نشد")


def free_slots(day: str, duration_minutes: int, work_start: str | None = None,
               work_end: str | None = None) -> list[str]:
    ws = work_start or os.environ.get("WORK_START", "09:00")
    we = work_end or os.environ.get("WORK_END", "17:00")
    d = parse_dt(day[:10] + "T00:00").date()
    start = parse_dt(f"{d}T{ws}")
    end = parse_dt(f"{d}T{we}")
    start = max(start, now())
    busy = [(parse_dt(m["start"]), parse_dt(m["end"]))
            for m in list_meetings(fmt(start), fmt(end))]
    busy.sort()
    slots, cur, step = [], start, timedelta(minutes=30)
    # از ابتدای نیم‌ساعت بعدی شروع کن
    if cur.minute % 30:
        cur += timedelta(minutes=30 - cur.minute % 30)
    need = timedelta(minutes=duration_minutes)
    while cur + need <= end:
        if all(not (b0 < cur + need and b1 > cur) for b0, b1 in busy):
            slots.append(fmt(cur))
        cur += step
    return slots


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_ics(meetings: list[dict], organizer: str, method: str = "REQUEST") -> str:
    def utc(v: str) -> str:
        return parse_dt(v).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//email-meeting-agent//FA", f"METHOD:{method}"]
    for m in meetings:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{m['id']}@email-meeting-agent",
            f"SEQUENCE:{m.get('sequence', 0)}",
            f"DTSTAMP:{utc(fmt(now()))}",
            f"DTSTART:{utc(m['start'])}",
            f"DTEND:{utc(m['end'])}",
            f"SUMMARY:{_ics_escape(m['title'])}",
            f"ORGANIZER:mailto:{organizer}",
            "STATUS:" + ("CANCELLED" if m.get("status") == "cancelled" else "CONFIRMED"),
        ]
        if m.get("location"):
            lines.append(f"LOCATION:{_ics_escape(m['location'])}")
        if m.get("description"):
            lines.append(f"DESCRIPTION:{_ics_escape(m['description'])}")
        for a in m.get("attendees", []):
            lines.append(f"ATTENDEE;RSVP=TRUE:mailto:{a}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------- پیش‌نویس‌ها (منتظر تأیید کاربر) ----------

def list_drafts(status: str = "pending") -> list[dict]:
    return [d for d in _load(DRAFTS) if d["status"] == status]


def add_draft(to: str, subject: str, body: str, cc: str = "", in_reply_to: str = "",
              references: str = "", reply_to_id: str = "", meeting_id: str = "",
              ics_method: str = "") -> dict:
    d = {
        "id": uuid.uuid4().hex[:8], "to": to, "cc": cc, "subject": subject, "body": body,
        "in_reply_to": in_reply_to, "references": references, "reply_to_id": reply_to_id,
        "meeting_id": meeting_id, "ics_method": ics_method,
        "status": "pending", "created": fmt(now()),
    }
    with _lock:
        items = _load(DRAFTS)
        items.append(d)
        _save(DRAFTS, items)
    return d


def update_draft(did: str, **changes) -> dict:
    with _lock:
        items = _load(DRAFTS)
        for d in items:
            if d["id"] == did:
                d.update({k: v for k, v in changes.items() if v is not None})
                _save(DRAFTS, items)
                return d
    raise KeyError(f"پیش‌نویس {did} پیدا نشد")


def get_draft(did: str) -> dict:
    for d in _load(DRAFTS):
        if d["id"] == did:
            return d
    raise KeyError(f"پیش‌نویس {did} پیدا نشد")
