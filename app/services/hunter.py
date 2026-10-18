"""Hunter.io fallback: domain search when the crawler finds no address."""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)
BASE = "https://api.hunter.io/v2"


def domain_search(domain: str) -> list[dict]:
    """Return list of {email, first_name, last_name, position, type, confidence}."""
    key = get_settings().hunter_api_key
    if not key or not domain:
        return []
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{BASE}/domain-search", params={"domain": domain, "limit": 5, "api_key": key})
        if r.status_code != 200:
            log.warning("Hunter domain-search %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json().get("data", {})
        return data.get("emails", []) or []


def pick_best(emails: list[dict]) -> dict | None:
    if not emails:
        return None
    emails = sorted(emails, key=lambda e: (e.get("type") == "personal", e.get("confidence", 0)), reverse=True)
    return emails[0]
