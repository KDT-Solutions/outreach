"""SMTP2GO HTTP API: send mail, parse webhook events."""

from __future__ import annotations

import html
import logging
import uuid

import httpx

log = logging.getLogger(__name__)
SEND_URL = "https://api.smtp2go.com/v3/email/send"


class SendError(RuntimeError):
    pass


def send(
    api_key: str,
    sender: str,
    to: str,
    subject: str,
    text_body: str,
    reply_to: str | None,
    unsubscribe_url: str,
    unsubscribe_mailto: str,
    sender_domain: str,
) -> tuple[str, str]:
    """Send one mail. Returns (provider email_id, Message-ID header)."""
    message_id = f"<{uuid.uuid4()}@{sender_domain}>"
    headers = [
        {"header": "Message-ID", "value": message_id},
        {"header": "List-Unsubscribe", "value": f"<{unsubscribe_url}>, <mailto:{unsubscribe_mailto}>"},
        {"header": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        {"header": "Precedence", "value": "bulk"},
    ]
    if reply_to:
        headers.append({"header": "Reply-To", "value": reply_to})

    payload = {
        "sender": sender,
        "to": [to],
        "subject": subject,
        "text_body": text_body,
        "html_body": _to_html(text_body),
        "custom_headers": headers,
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(
            SEND_URL,
            json=payload,
            headers={"X-Smtp2go-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
    try:
        data = r.json()
    except ValueError:
        raise SendError(f"SMTP2GO {r.status_code}: {r.text[:300]}")
    if r.status_code != 200 or data.get("data", {}).get("failed", 0) or not data.get("data", {}).get("succeeded"):
        raise SendError(f"SMTP2GO {r.status_code}: {data}")
    return str(data["data"].get("email_id", "")), message_id


def _to_html(text: str) -> str:
    paras = [p.strip() for p in text.split("\n\n")]
    body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras if p)
    return (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.5;color:#222">' + body + "</body></html>"
    )


# Webhook events SMTP2GO can post: processed, delivered, bounce, spam, unsubscribe, open, click, reject
SUPPRESS_EVENTS = {"bounce": "bounce", "spam": "spam", "unsubscribe": "unsubscribe", "reject": "bounce"}


def parse_webhook(payload: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (event, recipient, email_id, message_id) from a webhook payload."""
    event = (payload.get("event") or payload.get("type") or "").lower() or None
    rcpt = payload.get("rcpt") or payload.get("recipient") or payload.get("to") or payload.get("email")
    if isinstance(rcpt, list):
        rcpt = rcpt[0] if rcpt else None
    email_id = payload.get("email_id") or payload.get("sendid") or payload.get("message-id")
    message_id = payload.get("Message-Id") or payload.get("message_id") or payload.get("message-id")
    if event == "bounce" and str(payload.get("bounce_type", "")).lower().startswith("soft"):
        event = "soft_bounce"
    return event, (rcpt.lower() if isinstance(rcpt, str) else None), email_id, message_id
