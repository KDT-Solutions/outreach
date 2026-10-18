"""Cheap deliverability checks: syntax + MX record. No SMTP probing (often blocked, and
probing SMEs' mail servers from a sending IP hurts reputation)."""

from __future__ import annotations

import re
from functools import lru_cache

import dns.resolver

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)


def syntax_ok(email: str) -> bool:
    return bool(email and EMAIL_RE.match(email))


@lru_cache(maxsize=4096)
def mx_ok(domain: str) -> bool:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        return len(answers) > 0
    except Exception:
        try:
            dns.resolver.resolve(domain, "A", lifetime=6)
            return True  # implicit MX via A record
        except Exception:
            return False


def check(email: str) -> bool:
    if not syntax_ok(email):
        return False
    return mx_ok(email.rsplit("@", 1)[1].lower())
