"""search_leads and enrich_leads: find companies and their mail address for a campaign.

Both use the existing building blocks: places.search_text/details for Google Places and
pipeline.find_email (crawler + Hunter fallback) with the API crawl mode: generic mailboxes
first (info@, kontakt@, office@, mail@), only addresses on the company's own domain, at most
api_crawl_max_pages pages per domain and api_crawl_timeout seconds per page.

Network work runs in a thread pool without DB access; the results are written in the
request thread. One failing entry is reported in the result, it never aborts the call."""

from __future__ import annotations

import dataclasses
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import pipeline
from ..config import get_settings
from ..models import Campaign, Lead, Suppression
from ..services import crawler, places, verify
from . import schemas
from .service import ApiError, Call, _campaign, _preview

NO_ADDRESS = "keine Adresse gefunden"
NATIONWIDE = {"schweiz", "suisse", "svizzera", "switzerland", "ch"}
LEGAL_FORMS = {"ag", "gmbh", "sa", "sarl", "sagl", "kg", "ksg", "inc", "ltd", "co", "und", "et", "the", "die", "der", "le", "la"}
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "é": "e", "è": "e", "ê": "e", "à": "a", "â": "a", "ç": "c", "ô": "o"})


@dataclass
class Prospect:
    index: int
    firma: str
    ort: str | None = None
    plz: str | None = None
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    place_id: str | None = None
    types: list[str] = field(default_factory=list)
    email: str | None = None
    source: str | None = None
    mx_ok: bool | None = None
    candidates: list[str] = field(default_factory=list)
    site_excerpt: str | None = None
    impressum_excerpt: str | None = None
    contact_name: str | None = None
    note: str | None = None
    error: str | None = None


# ---------------------------------------------------------------- conversion and matching
def from_place(index: int, hit: places.PlaceHit, website: str | None = None, phone: str | None = None) -> Prospect:
    """A Places hit as lead candidate: firma, ort (from the address), website, phone."""
    plz, ort = places.city_of(hit.address)
    return Prospect(index=index, firma=hit.name[:200], ort=ort, plz=plz, address=hit.address, website=website,
                    phone=phone, place_id=hit.place_id, types=list(hit.types))


def _name_tokens(name: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", name.lower().translate(_UMLAUTS))
    return {w for w in words if len(w) > 1 and w not in LEGAL_FORMS}


def names_match(wanted: str, found: str) -> bool:
    """"Muster AG" matches "Muster AG Basel" and "Garage Muster"; "Muster" does not match "Beispiel"."""
    a, b = _name_tokens(wanted), _name_tokens(found)
    return bool(a and b) and (a <= b or b <= a)


def lead_note(p: Prospect) -> str:
    parts = []
    if p.phone:
        parts.append(f"Tel. {p.phone}")
    if p.note:
        parts.append(p.note)
    return "; ".join(parts)


# ---------------------------------------------------------------- network work (threads, no DB)
def _crawl_opts() -> dict:
    s = get_settings()
    return {"prefer_generic": True, "own_domain_only": True, "max_pages": s.api_crawl_max_pages, "timeout": s.api_crawl_timeout}


def _fill_website(p: Prospect, language: str) -> None:
    if p.website or not p.place_id:
        return
    p.website, phone = places.details(p.place_id)
    p.phone = p.phone or phone


def _resolve_by_name(p: Prospect, language: str) -> None:
    """No website given: Places search for firma + plz + ort, only an exact name match counts."""
    query = " ".join(x for x in (p.firma, p.plz, p.ort) if x)
    for hit in places.search_text(query, max_results=3, language=language):
        if names_match(p.firma, hit.name):
            plz, ort = places.city_of(hit.address)
            p.place_id, p.address, p.types = hit.place_id, hit.address, list(hit.types)
            p.plz, p.ort = p.plz or plz, p.ort or ort
            _fill_website(p, language)
            return
    p.note = "keine Webseite gefunden"


def _discover(p: Prospect, language: str, resolve: bool) -> Prospect:
    try:
        if resolve and not p.website:
            _resolve_by_name(p, language)
        _fill_website(p, language)
        if not p.website:
            p.note = p.note or "keine Webseite"
            return p
        hint = p.address or " ".join(x for x in (p.plz, p.ort) if x) or None
        found = pipeline.find_email(p.website, place_hint=hint, **_crawl_opts())
        res = found.crawl
        p.candidates = res.emails[:10]
        p.site_excerpt, p.impressum_excerpt = res.site_excerpt or None, res.impressum_excerpt or None
        p.contact_name = found.contact_name
        if not found.email:
            p.note = res.error
            return p
        p.email, p.source = found.email, found.source
        p.mx_ok = verify.check(found.email)
        if not p.mx_ok:
            p.note = f"{found.email}: kein MX-Eintrag"
    except Exception as exc:  # one entry must never break the call
        p.error = f"{type(exc).__name__}: {exc}"[:300]
    return p


def _parallel(items: list[Prospect], fn: Callable[[Prospect], Prospect]) -> list[Prospect]:
    """Run fn on copies in a pool; entries that miss the call's time budget get an error."""
    s = get_settings()
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=max(1, s.api_prospect_workers))
    futures = {pool.submit(fn, dataclasses.replace(p)): p for p in items}
    done, _ = wait(futures, timeout=s.api_prospect_deadline_seconds)
    pool.shutdown(wait=False, cancel_futures=True)
    out = []
    for fut, original in futures.items():
        if fut in done:
            out.append(fut.result())
        else:
            out.append(dataclasses.replace(original, error="Zeitlimit des Aufrufs erreicht, Eintrag nicht bearbeitet"))
    return sorted(out, key=lambda p: p.index)


# ---------------------------------------------------------------- duplicates and storage
def _known(db: Session, c: Campaign, prospects: list[Prospect]) -> tuple[dict, dict, set]:
    """(place_id -> lead_id, email -> lead_id, suppressed values) for the campaign's tenant."""
    pids = {p.place_id for p in prospects if p.place_id}
    emails = {p.email for p in prospects if p.email}
    by_place = dict(db.execute(select(Lead.place_id, Lead.id).where(Lead.tenant_id == c.tenant_id, Lead.place_id.in_(pids))).all()) if pids else {}
    by_email = (
        dict(db.execute(select(func.lower(Lead.email), Lead.id).where(Lead.tenant_id == c.tenant_id, func.lower(Lead.email).in_(emails))).all())
        if emails else {}
    )
    keys = emails | {"@" + e.rsplit("@", 1)[1] for e in emails}
    suppressed = set(db.scalars(select(Suppression.value).where(Suppression.tenant_id == c.tenant_id, Suppression.value.in_(keys))).all()) if keys else set()
    return by_place, by_email, suppressed


def classify(db: Session, c: Campaign, prospects: list[Prospect]) -> list[dict]:
    """Decide per entry, same rules as import_leads: existing e-mail or place at the tenant,
    duplicate inside the call, suppressed address or domain. Returns result rows; rows with
    status enriched/no_email are the ones to create."""
    by_place, by_email, suppressed = _known(db, c, prospects)
    seen_place: dict[str, int] = {}
    seen_email: dict[str, int] = {}
    rows = []
    for p in prospects:
        row = {"index": p.index, "firma": p.firma, "ort": p.ort, "website": p.website, "email": p.email, "email_source": p.source}
        if p.place_id and p.place_id in by_place:
            row.update(status="duplicate", reason="exists", lead_id=by_place[p.place_id])
        elif p.place_id and p.place_id in seen_place:
            row.update(status="duplicate", reason="duplicate_in_request", first_index=seen_place[p.place_id])
        elif p.error:
            row.update(status="error", error=p.error)
        elif p.email and p.email in by_email:
            row.update(status="duplicate", reason="exists", lead_id=by_email[p.email])
        elif p.email and p.email in seen_email:
            row.update(status="duplicate", reason="duplicate_in_request", first_index=seen_email[p.email])
        elif p.email and (p.email in suppressed or "@" + p.email.rsplit("@", 1)[1] in suppressed):
            row.update(status="duplicate", reason="suppressed")
        elif p.email and p.mx_ok:
            row["status"] = "enriched"
        else:
            p.note = NO_ADDRESS + (f" ({p.note})" if p.note else "")
            row.update(status="no_email", note=p.note)
        if p.place_id:
            seen_place.setdefault(p.place_id, p.index)
        if p.email:
            seen_email.setdefault(p.email, p.index)
        rows.append(row)
    return rows


def _store(db: Session, c: Campaign, prospects: list[Prospect], rows: list[dict], source_query: str, language: str | None) -> list[int]:
    by_index = {p.index: p for p in prospects}
    created = []
    for row in rows:
        if row["status"] not in ("enriched", "no_email"):
            continue
        p = by_index[row["index"]]
        domain = crawler.domain_of(p.website)
        lead = Lead(
            tenant_id=c.tenant_id, campaign_id=c.id, place_id=p.place_id, company=p.firma[:200], city=p.ort, address=p.address,
            phone=p.phone, website=p.website, domain=domain, place_types=",".join(p.types)[:300] or None,
            source_query=source_query[:200], language=language, email=p.email, email_source=p.source, email_mx_ok=p.mx_ok,
            email_candidates=",".join(p.candidates) or None, site_excerpt=p.site_excerpt, impressum_excerpt=p.impressum_excerpt,
            contact_name=p.contact_name, status=row["status"], notes=lead_note(p),
        )
        db.add(lead)
        db.flush()
        row["lead_id"] = lead.id
        created.append(lead.id)
    return created


def _counts(rows: list[dict]) -> dict:
    return {
        "found": len(rows),
        # in a preview: would be created
        "created": sum(1 for r in rows if r["status"] == "enriched"),
        "no_email": sum(1 for r in rows if r["status"] == "no_email"),
        "duplicates": sum(1 for r in rows if r["status"] == "duplicate"),
        "errors": sum(1 for r in rows if r["status"] == "error"),
    }


# ---------------------------------------------------------------- tools
def _places_hits(data: schemas.LeadSearch) -> tuple[list[places.PlaceHit], dict]:
    region = data.region.strip()
    info = {"query": data.query, "region": region, "radius_km": data.radius_km, "radius_applied": False}
    try:
        if region.lower() in NATIONWIDE:
            hits = places.search_text(data.query, max_results=min(60, data.limit), language=data.language)
            info["radius_km"] = None
        else:
            center = places.locate(region, language=data.language)
            bias = (center[0], center[1], data.radius_km * 1000.0) if center else None
            hits = places.search_text(f"{data.query} {region}", max_results=60, language=data.language, bias=bias)
            if center:
                info["radius_applied"] = True
                hits = [h for h in hits if h.lat is None or places.distance_km(center[0], center[1], h.lat, h.lng) <= data.radius_km]
            else:
                info["note"] = "Region nicht gefunden, Umkreis nicht angewendet"
    except places.PlacesError as exc:
        raise ApiError(502, "places_error", str(exc)[:300])
    info["places_limit"] = "Google Places liefert pro Suche höchstens 60 Treffer"
    return hits[: data.limit], info


def search_leads(db: Session, call: Call, campaign_id: int, data: schemas.LeadSearch) -> dict:
    c = _campaign(db, call, campaign_id)
    hits, info = _places_hits(data)
    prospects = [from_place(i, h) for i, h in enumerate(hits)]
    by_place, _, _ = _known(db, c, prospects)
    fresh = [p for p in prospects if p.place_id not in by_place]
    call.detail = f"{data.query} / {data.region}: {len(hits)} Treffer"

    if not data.confirm:
        # websites for the list (Place Details), no crawling, nothing written
        def details_only(p: Prospect) -> Prospect:
            try:
                _fill_website(p, data.language)
            except Exception as exc:
                p.error = f"{type(exc).__name__}: {exc}"[:300]
            return p

        done = {p.index: p for p in _parallel(fresh, details_only)}
        listing = []
        for p in prospects:
            q = done.get(p.index, p)
            item = {"index": p.index, "firma": q.firma, "ort": q.ort, "website": q.website}
            if p.place_id in by_place:
                item.update(status="duplicate", reason="exists", lead_id=by_place[p.place_id])
            elif q.error:
                item.update(status="error", error=q.error)
            listing.append(item)
        return _preview({"search": info, "found": len(listing), "hits": listing})

    done = {p.index: p for p in _parallel(fresh, lambda p: _discover(p, data.language, resolve=False))}
    final = [done.get(p.index, p) for p in prospects]
    rows = classify(db, c, final)
    created = _store(db, c, final, rows, f"{data.query} | {data.region}", data.language)
    pipeline.log_event(db, "api_search_leads", f"{c.name}: {call.detail}, {len(created)} angelegt", c.tenant_id)
    call.ids["lead_ids"] = created
    return {"confirmed": True, "search": info, **_counts(rows), "leads": rows}


def enrich_leads(db: Session, call: Call, campaign_id: int, data: schemas.LeadEnrich) -> dict:
    c = _campaign(db, call, campaign_id)
    prospects = [Prospect(index=i, firma=e.firma, ort=e.ort, plz=e.plz, website=e.website) for i, e in enumerate(data.leads)]
    language = c.language or "de"
    final = _parallel(prospects, lambda p: _discover(p, language, resolve=True))
    rows = classify(db, c, final)
    call.detail = f"{len(final)} Einträge"
    if not data.confirm:
        return _preview({**_counts(rows), "leads": rows})
    _store(db, c, final, rows, "enrich_leads", None)
    created = [r["lead_id"] for r in rows if r.get("lead_id")]
    pipeline.log_event(db, "api_enrich_leads", f"{c.name}: {len(created)} angelegt aus {len(final)} Einträgen", c.tenant_id)
    call.ids["lead_ids"] = created
    return {"confirmed": True, **_counts(rows), "leads": rows}
