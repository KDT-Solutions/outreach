"""Create a Zammad ticket for a reply so the follow-up happens in the helpdesk."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def create_ticket(url: str, token: str, group: str, customer_email: str, title: str, body: str, note: str | None = None) -> int | None:
    payload = {
        "title": title[:200],
        "group": group,
        "customer": customer_email,
        "article": {
            "subject": title[:200],
            "body": body,
            "type": "email",
            "sender": "Customer",
            "internal": False,
            "content_type": "text/plain",
        },
    }
    headers = {"Authorization": f"Token token={token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as client:
        r = client.post(url.rstrip("/") + "/api/v1/tickets", json=payload, headers=headers)
        if r.status_code not in (200, 201):
            log.warning("Zammad ticket failed %s: %s", r.status_code, r.text[:300])
            return None
        ticket_id = r.json().get("id")
        if note and ticket_id:
            client.post(
                url.rstrip("/") + "/api/v1/ticket_articles",
                json={"ticket_id": ticket_id, "body": note, "type": "note", "internal": True, "content_type": "text/plain"},
                headers=headers,
            )
        return ticket_id
