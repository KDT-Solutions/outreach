"""Fixed-text sequence steps: plain-text subject and body with {{placeholder}} fields.

A campaign step with a "body_text" key is rendered from the template instead of being
written by the LLM. Only the placeholders below exist; anything else is rejected when
the sequence is saved."""

from __future__ import annotations

import re

from ..models import Lead

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Lead attribute behind each placeholder
PLACEHOLDERS = {
    "anrede": "salutation",
    "vorname": "first_name",
    "nachname": "last_name",
    "firma": "company",
    "ort": "city",
    "email": "email",
}
HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>")
# placeholders allowed in signatures (tenant per language or campaign override)
SIGNATURE_PLACEHOLDERS = ("sender_name", "sender_email", "reply_to", "unsubscribe_url")


def is_template_step(step: dict) -> bool:
    return bool(step.get("body_text"))


def placeholders_in(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text or "")))


def check_template(text: str, allowed=PLACEHOLDERS) -> list[str]:
    """Problems with a template string: unknown placeholders, stray braces, HTML."""
    problems = []
    unknown = [p for p in placeholders_in(text) if p not in allowed]
    if unknown:
        problems.append("Unbekannte Platzhalter: " + ", ".join("{{" + u + "}}" for u in unknown) + "; erlaubt: " + ", ".join("{{" + k + "}}" for k in allowed))
    rest = PLACEHOLDER_RE.sub("", text or "")
    if "{{" in rest or "}}" in rest:
        problems.append("Ungültige Platzhalter-Syntax, erwartet {{name}}")
    if HTML_TAG_RE.search(text or ""):
        problems.append("Kein HTML erlaubt, nur Klartext")
    return problems


def values_for(lead: Lead) -> dict[str, str]:
    return {name: (getattr(lead, attr, None) or "").strip() for name, attr in PLACEHOLDERS.items()}


def render(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), text or "")


def render_step(step: dict, lead: Lead) -> tuple[str, str, list[str]]:
    """Returns (subject, body, placeholders that rendered empty)."""
    values = values_for(lead)
    used = placeholders_in(step.get("subject", "")) + placeholders_in(step.get("body_text", ""))
    missing = [p for p in dict.fromkeys(used) if not values.get(p)]
    subject = " ".join(render(step.get("subject", ""), values).split())
    body = render(step.get("body_text", ""), values).strip()
    return subject, body, missing
