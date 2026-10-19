"""Zammad: read new tickets (replies land there via the tenant mailbox) and add notes."""

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


def _headers(token: str) -> dict:
    return {"Authorization": f"Token token={token}", "Content-Type": "application/json"}


def recent_tickets(url: str, token: str, since_iso: str, group: str | None = None, limit: int = 100) -> list[dict]:
    """Tickets created after since_iso (UTC, e.g. 2026-10-19T12:00:00Z), expanded.

    Zammad's search only takes a date in the range query, so we search from the day
    before and filter the exact timestamp client-side. With expand=true, "customer"
    is the customer's e-mail as a plain string."""
    day = since_iso[:10]
    q = f"created_at:>={day}"
    if group:
        q += f' AND group.name:"{group}"'
    with httpx.Client(timeout=30) as client:
        r = client.get(
            url.rstrip("/") + "/api/v1/tickets/search",
            params={"query": q, "limit": limit, "expand": "true", "sort_by": "created_at", "order_by": "asc"},
            headers=_headers(token),
        )
        if r.status_code != 200:
            log.warning("Zammad search %s: %s", r.status_code, r.text[:300])
            return []
        data = r.json()
        tickets = data if isinstance(data, list) else data.get("tickets", []) or []
        return [t for t in tickets if (t.get("created_at") or "") > since_iso.replace("Z", "")]


def first_article(url: str, token: str, ticket_id: int) -> dict | None:
    with httpx.Client(timeout=30) as client:
        r = client.get(url.rstrip("/") + f"/api/v1/ticket_articles/by_ticket/{ticket_id}", headers=_headers(token))
        if r.status_code != 200:
            return None
        arts = r.json() or []
        return arts[0] if arts else None


def add_note(url: str, token: str, ticket_id: int, body: str) -> bool:
    with httpx.Client(timeout=30) as client:
        r = client.post(
            url.rstrip("/") + "/api/v1/ticket_articles",
            json={"ticket_id": ticket_id, "body": body, "type": "note", "internal": True, "content_type": "text/plain"},
            headers=_headers(token),
        )
        return r.status_code in (200, 201)
