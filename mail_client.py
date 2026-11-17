"""دسترسی به صندوق ایمیل: IMAP برای خواندن، SMTP برای ارسال.

اگر IMAP_HOST در .env تنظیم نشده باشد، حالت نمایشی (demo) با ایمیل‌های نمونه
از data/demo_inbox.json اجرا می‌شود و ارسال واقعی انجام نمی‌شود.
"""

from __future__ import annotations

import email
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO_FILE = ROOT / "data" / "demo_inbox.json"
DEMO_SENT = ROOT / "data" / "demo_sent.json"


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def is_demo() -> bool:
    return not cfg("IMAP_HOST")


def my_address() -> str:
    return cfg("EMAIL_ADDRESS") or cfg("IMAP_USER") or "you@example.com"


# ---------- تبدیل پیام ----------

def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = _html_to_text(text)
    return text.strip()


def _attachments(msg: EmailMessage) -> list[str]:
    return [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]


def _date_iso(value: str | None) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat(timespec="minutes")
    except Exception:
        return value


def _summary(uid: str, msg: EmailMessage, seen: bool) -> dict:
    body = _body_text(msg)
    return {
        "id": uid,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "")),
        "date": _date_iso(msg.get("Date")),
        "unread": not seen,
        "snippet": re.sub(r"\s+", " ", body)[:160],
    }


# ---------- حالت نمایشی ----------

def _demo_load() -> list[dict]:
    if not DEMO_FILE.exists():
        return []
    return json.loads(DEMO_FILE.read_text(encoding="utf-8"))


def _demo_save(items: list[dict]) -> None:
    DEMO_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _demo_summary(m: dict) -> dict:
    return {
        "id": m["id"], "from": m["from"], "to": m.get("to", my_address()),
        "subject": m["subject"], "date": m["date"], "unread": m.get("unread", True),
        "snippet": re.sub(r"\s+", " ", m["body"])[:160],
    }


# ---------- IMAP ----------

def _imap() -> imaplib.IMAP4:
    host = cfg("IMAP_HOST")
    port = int(cfg("IMAP_PORT", "993"))
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context()) if port == 993 \
        else imaplib.IMAP4(host, port)
    conn.login(cfg("IMAP_USER") or my_address(), cfg("IMAP_PASSWORD"))
    return conn


def _select(conn: imaplib.IMAP4, folder: str, readonly: bool = True) -> None:
    typ, _ = conn.select(f'"{folder}"', readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"پوشهٔ «{folder}» باز نشد")


def _fetch(conn: imaplib.IMAP4, uids: list[bytes]) -> list[dict]:
    out = []
    for uid in uids:
        typ, data = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        flags = data[0][0].decode(errors="ignore")
        msg = email.message_from_bytes(data[0][1], policy=policy.default)
        out.append(_summary(uid.decode(), msg, "\\Seen" in flags))
    return out


def _uid_search(conn: imaplib.IMAP4, criteria: list[str], text: str | None = None) -> list[bytes]:
    if text:
        if text.isascii():
            typ, data = conn.uid("SEARCH", *criteria, "TEXT", f'"{text}"')
        else:
            conn.literal = text.encode("utf-8")
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria, "TEXT")
    else:
        typ, data = conn.uid("SEARCH", *criteria)
    if typ != "OK":
        return []
    return data[0].split()


# ---------- API عمومی ----------

def list_messages(folder: str = "INBOX", limit: int = 20, unread_only: bool = False,
                  query: str | None = None) -> list[dict]:
    limit = max(1, min(limit, 50))
    if is_demo():
        items = _demo_load()
        if unread_only:
            items = [m for m in items if m.get("unread", True)]
        if query:
            q = query.lower()
            items = [m for m in items if q in (m["subject"] + m["body"] + m["from"]).lower()]
        items.sort(key=lambda m: m["date"], reverse=True)
        return [_demo_summary(m) for m in items[:limit]]

    conn = _imap()
    try:
        _select(conn, folder)
        uids = _uid_search(conn, ["UNSEEN"] if unread_only else ["ALL"], query)
        return list(reversed(_fetch(conn, uids[-limit:])))
    finally:
        conn.logout()


def get_message(msg_id: str, folder: str = "INBOX") -> dict:
    if is_demo():
        for m in _demo_load():
            if m["id"] == msg_id:
                return {**_demo_summary(m), "body": m["body"], "cc": m.get("cc", ""),
                        "message_id": m.get("message_id", f"<{m['id']}@demo>"),
                        "attachments": m.get("attachments", [])}
        raise KeyError(f"ایمیل {msg_id} پیدا نشد")

    conn = _imap()
    try:
        _select(conn, folder)
        typ, data = conn.uid("FETCH", msg_id.encode(), "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise KeyError(f"ایمیل {msg_id} پیدا نشد")
        flags = data[0][0].decode(errors="ignore")
        msg = email.message_from_bytes(data[0][1], policy=policy.default)
        body = _body_text(msg)
        return {**_summary(msg_id, msg, "\\Seen" in flags), "body": body[:20000],
                "cc": str(msg.get("Cc", "")), "message_id": str(msg.get("Message-ID", "")),
                "references": str(msg.get("References", "")), "attachments": _attachments(msg)}
    finally:
        conn.logout()


def mark_read(msg_id: str, read: bool = True, folder: str = "INBOX") -> None:
    if is_demo():
        items = _demo_load()
        for m in items:
            if m["id"] == msg_id:
                m["unread"] = not read
        _demo_save(items)
        return
    conn = _imap()
    try:
        _select(conn, folder, readonly=False)
        conn.uid("STORE", msg_id.encode(), "+FLAGS" if read else "-FLAGS", "(\\Seen)")
    finally:
        conn.logout()


def send(draft: dict, ics: str | None = None) -> str:
    """یک پیش‌نویس تأییدشده را می‌فرستد. فقط از مسیر تأیید کاربر صدا زده می‌شود."""
    msg = EmailMessage()
    name = cfg("EMAIL_NAME")
    msg["From"] = formataddr((name, my_address())) if name else my_address()
    msg["To"] = draft["to"]
    if draft.get("cc"):
        msg["Cc"] = draft["cc"]
    msg["Subject"] = draft["subject"]
    msg["Message-ID"] = make_msgid()
    if draft.get("in_reply_to"):
        msg["In-Reply-To"] = draft["in_reply_to"]
        msg["References"] = (draft.get("references", "") + " " + draft["in_reply_to"]).strip()
    msg.set_content(draft["body"])
    if ics:
        msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar",
                           filename="invite.ics", params={"method": "REQUEST"})

    if is_demo():
        sent = json.loads(DEMO_SENT.read_text(encoding="utf-8")) if DEMO_SENT.exists() else []
        sent.append({"to": draft["to"], "subject": draft["subject"], "body": draft["body"],
                     "invite": bool(ics)})
        DEMO_SENT.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")
        return "demo"

    host = cfg("SMTP_HOST") or cfg("IMAP_HOST").replace("imap", "smtp", 1)
    port = int(cfg("SMTP_PORT", "465"))
    user = cfg("SMTP_USER") or cfg("IMAP_USER") or my_address()
    password = cfg("SMTP_PASSWORD") or cfg("IMAP_PASSWORD")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
    return str(msg["Message-ID"])
