"""Lead search for the web UI: finds a lead from what a caller or a phone note gives you.

Matches company, e-mail, domain, website, phone, address, city, contact person, notes and
the waiting reason and the outcome note, across all campaigns. Punctuation, case and accents are
ignored ("HSK" finds "H.S.K CLES SERVICES", "cles" finds "Clés"); phone numbers match in
any notation (+41 21 831 00 01, 021 831 00 01, 0041218310001).

Every word must match. If nothing matches all words (e.g. a full company name copied from
a call note, "HSK - LES SERVICES CLES PLUS Sàrl"), the leads matching the most words are
returned instead, flagged as partial."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy.orm import Session, load_only

from ..models import Lead

LIMIT = 500
# a digit run this long is treated as a phone number
PHONE_MIN_DIGITS = 6
# national significant number: compare the last 9 digits (CH numbers have 9 after the 0 / +41)
PHONE_TAIL = 9
# words too common to rank a partial match on their own
STOPWORDS = {
    "ag", "sa", "gmbh", "sarl", "sagl", "ltd", "llc", "inc", "co", "und", "et", "and", "der", "die", "das",
    "le", "la", "les", "de", "du", "des", "il", "di", "von", "fur", "pour", "per", "hr", "herr", "frau", "mme", "m",
}

_FIELDS = (
    Lead.company, Lead.email, Lead.domain, Lead.website, Lead.phone, Lead.address, Lead.city,
    Lead.contact_name, Lead.first_name, Lead.last_name, Lead.notes, Lead.wait_reason, Lead.outcome_note,
)


def fold(text: str | None) -> str:
    """Lower case, accents removed, only letters and digits left."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def digits(text: str | None) -> str:
    return re.sub(r"\D+", "", text or "")


def _phone_key(text: str) -> str:
    d = digits(text)
    return d[-PHONE_TAIL:] if len(d) >= PHONE_MIN_DIGITS else ""


def terms(q: str) -> list[tuple[str, str]]:
    """Split the query into (kind, value): ("phone", last digits) or ("word", folded word).
    Phone numbers written with spaces ("+41 21 831 00 01") are joined first."""
    q = q or ""
    out: list[tuple[str, str]] = []
    # runs of digits with typical separators: +41 (0)21 831 00 01, 021/831.00.01
    for m in re.finditer(r"\+?\d[\d\s/().-]{4,}\d", q):
        key = _phone_key(m.group())
        if key:
            out.append(("phone", key))
            q = q.replace(m.group(), " ")
    for w in re.split(r"\s+", q):
        f = fold(w)
        if f:
            out.append(("word", f))
    return out


def _haystack(lead: Lead) -> tuple[str, str]:
    text = " ".join(fold(getattr(lead, col.key)) for col in _FIELDS)
    # every phone-like run in the lead (phone field, notes) as its last digits
    phones = " ".join(
        _phone_key(m.group()) for field in (lead.phone, lead.notes, lead.outcome_note, lead.wait_reason)
        for m in re.finditer(r"\+?\d[\d\s/().-]{4,}\d", field or "")
    )
    return text, phones


def _hit(kind: str, value: str, text: str, phones: str) -> bool:
    if kind == "phone":
        return value in phones
    return value in text


def search(db: Session, q: str, campaign_id: int | None = None, status: str | None = None,
           tenant_ids: list[int] | None = None, limit: int = LIMIT) -> tuple[list[Lead], bool]:
    """Returns (leads, partial). partial=True when no lead matched every word and the
    best partial matches are returned instead. tenant_ids restricts the search (API scope)."""
    wanted = terms(q)
    query = db.query(Lead).options(load_only(*_FIELDS, Lead.id, Lead.status, Lead.step, Lead.next_send_at,
                                             Lead.email_source, Lead.campaign_id, Lead.tenant_id, Lead.outcome_at))
    query = query.filter(Lead.deleted_at.is_(None))
    if tenant_ids is not None:
        query = query.filter(Lead.tenant_id.in_(tenant_ids))
    if campaign_id:
        query = query.filter(Lead.campaign_id == campaign_id)
    if status:
        query = query.filter(Lead.status == status)
    leads = query.order_by(Lead.id.desc()).all()
    if not wanted:
        return leads[:limit], False

    full, scored = [], []
    ranking = [t for t in wanted if t[0] == "phone" or (len(t[1]) >= 3 and t[1] not in STOPWORDS)]
    for lead in leads:
        text, phones = _haystack(lead)
        if all(_hit(k, v, text, phones) for k, v in wanted):
            full.append(lead)
        elif ranking:
            score = sum(1 for k, v in ranking if _hit(k, v, text, phones))
            # a phone number hit counts more than any single word
            score += sum(2 for k, v in ranking if k == "phone" and _hit(k, v, text, phones))
            if score:
                scored.append((score, lead))
    if full:
        return full[:limit], False
    scored.sort(key=lambda x: (-x[0], -x[1].id))
    return [lead for _, lead in scored][:limit], True
