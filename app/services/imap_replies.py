"""Poll a tenant mailbox over IMAP and return unseen messages that look like replies."""

from __future__ import annotations

import email
import imaplib
import logging
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr

log = logging.getLogger(__name__)


@dataclass
class Inbound:
    uid: str
    from_email: str
    subject: str
    body: str
    in_reply_to: str | None
    references: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                from bs4 import BeautifulSoup

                return BeautifulSoup(payload.decode(part.get_content_charset() or "utf-8", errors="replace"), "lxml").get_text(" ", strip=True)
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def fetch_unseen(host: str, port: int, user: str, password: str, folder: str = "INBOX", limit: int = 50) -> list[Inbound]:
    out: list[Inbound] = []
    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    try:
        conn.login(user, password)
        conn.select(folder)
        typ, data = conn.uid("search", None, "UNSEEN")
        if typ != "OK":
            return out
        uids = data[0].split()[-limit:]
        for uid in uids:
            typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            from_email = parseaddr(msg.get("From", ""))[1].lower()
            out.append(
                Inbound(
                    uid=uid.decode(),
                    from_email=from_email,
                    subject=_decode(msg.get("Subject")),
                    body=_body_text(msg)[:8000],
                    in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
                    references=msg.get("References", "") or "",
                )
            )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def mark_seen(host: str, port: int, user: str, password: str, folder: str, uids: list[str]) -> None:
    if not uids:
        return
    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    try:
        conn.login(user, password)
        conn.select(folder)
        for uid in uids:
            conn.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
