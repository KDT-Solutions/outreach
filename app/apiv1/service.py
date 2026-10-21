"""The automation tools. Each function works on a DB session for one authenticated caller
and returns plain JSON-able dicts; REST and MCP are thin wrappers around them.

Write tools return a preview ({"confirmed": false, "preview": ...}) and change nothing
unless confirm=true. Every write call, preview or not, lands in the api_audit table."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, pipeline
from ..config import get_settings
from ..db import SessionLocal
from ..models import LEAD_STATUSES, ApiAudit, Campaign, Lead, Message, Suppression, Tenant, TenantSignature
from ..services import signature, smtp2go, templating
from . import schemas
from .security import Principal

log = logging.getLogger(__name__)

# lead statuses that still lead to mails
ACTIVE_LEAD_STATUSES = ("new", "enriched", "needs_review", "scheduled", "in_sequence", "draft_failed", "waiting")
CAMPAIGN_STATUSES = ("draft", "active", "paused", "waiting", "done")
# API field -> Lead attribute
LEAD_FIELDS = {
    "firma": "company",
    "anrede": "salutation",
    "vorname": "first_name",
    "nachname": "last_name",
    "email": "email",
    "ort": "city",
    "notiz": "notes",
    "sprache": "language",
}
SAMPLE_LEAD = {"company": "Muster AG", "salutation": "Guten Tag Frau Muster", "first_name": "Erika", "last_name": "Muster", "city": "Zürich"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list | dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"error": err}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} nicht gefunden")


@dataclass
class Call:
    """One API call: who, over which transport, and what it touched (for the audit log)."""

    principal: Principal
    transport: str
    client_ip: str | None = None
    tenant_id: int | None = None
    ids: dict = field(default_factory=dict)
    detail: str = ""


def execute(tool: str, call: Call, fn: Callable[[Session, Call], dict], write: bool) -> dict:
    """Run fn in its own transaction. Previews are rolled back; write calls are audited."""
    db = SessionLocal()
    try:
        result = fn(db, call)
        if write:
            if not result.get("confirmed"):
                db.rollback()
            _audit(db, tool, call, "applied" if result.get("confirmed") else "preview")
        db.commit()
        return result
    except ApiError as exc:
        db.rollback()
        if write:
            call.detail = call.detail or f"{exc.code}: {exc.message}"
            _audit_separately(tool, call)
        raise
    except Exception:
        db.rollback()
        log.exception("API tool %s failed", tool)
        if write:
            call.detail = "internal_error"
            _audit_separately(tool, call)
        raise ApiError(500, "internal_error", "Interner Fehler")
    finally:
        db.close()


def _audit(db: Session, tool: str, call: Call, outcome: str) -> None:
    db.add(
        ApiAudit(
            token_id=call.principal.token_id, tenant_id=call.tenant_id, tool=tool, transport=call.transport,
            client_ip=call.client_ip, outcome=outcome, object_ids=call.ids, detail=call.detail[:2000],
        )
    )


def _audit_separately(tool: str, call: Call) -> None:
    try:
        with SessionLocal() as db:
            _audit(db, tool, call, "error")
            db.commit()
    except Exception:
        log.exception("audit write failed for %s", tool)


def _preview(data: dict) -> dict:
    return {"confirmed": False, "preview": data, "hint": "Nichts geändert. Mit confirm=true ausführen."}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- scoping
def _tenants(db: Session, call: Call) -> list[Tenant]:
    p = call.principal
    q = db.query(Tenant)
    if p.tenants is not None:
        if not p.tenants:
            return []
        q = q.filter(Tenant.slug.in_(p.tenants))
    return q.order_by(Tenant.name).all()


def _tenant(db: Session, call: Call, slug: str | None) -> Tenant:
    tenants = _tenants(db, call)
    if slug is None:
        if len(tenants) != 1:
            raise ApiError(400, "tenant_required", "Mandant angeben (tenant), der Token hat Zugriff auf mehrere oder keine Mandanten")
        tenant = tenants[0]
    else:
        tenant = next((t for t in tenants if t.slug == slug.strip().lower()), None)
        if tenant is None:
            raise not_found("Mandant")
    call.tenant_id = tenant.id
    return tenant


def _campaign(db: Session, call: Call, campaign_id: int) -> Campaign:
    c = db.get(Campaign, campaign_id)
    if c is None or not call.principal.may_access(c.tenant.slug):
        raise not_found("Kampagne")
    call.tenant_id = c.tenant_id
    call.ids.setdefault("campaign_id", c.id)
    return c


def _lead(db: Session, call: Call, lead_id: int, campaign: Campaign | None = None) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.deleted_at is not None or not call.principal.may_access(lead.campaign.tenant.slug):
        raise not_found("Lead")
    if campaign is not None and lead.campaign_id != campaign.id:
        raise not_found("Lead")
    call.tenant_id = lead.tenant_id
    call.ids.setdefault("lead_id", lead.id)
    call.ids.setdefault("campaign_id", lead.campaign_id)
    return lead


# ---------------------------------------------------------------- serialisation (never secrets)
def tenant_dict(t: Tenant) -> dict:
    return {
        "id": t.id, "slug": t.slug, "name": t.name, "active": t.active,
        "sender_name": t.from_name, "sender_email": t.from_email, "reply_to": t.reply_to,
        "daily_limit": t.daily_limit, "send_window": {"start_hour": t.send_hour_start, "end_hour": t.send_hour_end, "weekdays_only": t.send_weekdays_only, "timezone": t.timezone},
        "smtp_configured": bool(t.smtp2go_api_key_enc or get_settings().smtp2go_api_key),
        "zammad_configured": bool(t.zammad_url and t.zammad_token_enc),
    }


def steps_out(steps: list | None) -> list[dict]:
    out = []
    for i, s in enumerate(steps or []):
        d = {"step": i + 1, "delay_days": int(s.get("delay_days", 0))}
        if templating.is_template_step(s):
            d.update(mode="template", subject=s.get("subject", ""), body_text=s["body_text"])
        else:
            d.update(mode="llm", instruction=s.get("instruction", ""))
        out.append(d)
    return out


def _lead_counts(db: Session, campaign_id: int) -> dict[str, int]:
    rows = db.execute(
        select(Lead.status, func.count()).where(Lead.campaign_id == campaign_id, Lead.deleted_at.is_(None)).group_by(Lead.status)
    ).all()
    return {status: n for status, n in rows}


def campaign_dict(db: Session, c: Campaign, full: bool = False) -> dict:
    name, email, reply_to = c.sender
    d = {
        "id": c.id, "tenant": c.tenant.slug, "name": c.name, "status": c.status, "language": c.language,
        "auto_approve": c.auto_approve, "sender_name": name, "sender_email": email, "reply_to": reply_to,
        "created_at": _iso(c.created_at),
    }
    if c.status == "waiting":
        d["wait_reason"] = c.wait_reason
    if full:
        d["signature_override"] = c.signature_override
        d["unsubscribe_text_override"] = c.unsubscribe_text_override
        d["steps"] = steps_out(c.steps)
        d["lead_counts"] = _lead_counts(db, c.id)
    return d


def lead_dict(l: Lead) -> dict:
    d = {api: getattr(l, attr) for api, attr in LEAD_FIELDS.items()}
    d.update(
        id=l.id, campaign_id=l.campaign_id, status=l.status, steps_sent=l.step, email_source=l.email_source,
        email_mx_ok=l.email_mx_ok, next_send_at=_iso(l.next_send_at), last_sent_at=_iso(l.last_sent_at),
        last_error=l.last_error, created_at=_iso(l.created_at),
    )
    if l.status == "waiting":
        d.update(wait_reason=l.wait_reason, status_after_release=l.wait_prev_status)
    if l.status == "unsubscribed" or l.unsubscribe_reason:
        d.update(unsubscribe_reason=l.unsubscribe_reason, unsubscribe_comment=l.unsubscribe_comment, unsubscribed_at=_iso(l.unsubscribed_at), unsubscribe_source=l.unsubscribe_source)
    return d


# ---------------------------------------------------------------- read tools
def list_tenants(db: Session, call: Call) -> dict:
    return {"tenants": [tenant_dict(t) for t in _tenants(db, call)]}


def list_campaigns(db: Session, call: Call, tenant: str | None = None) -> dict:
    tenants = [_tenant(db, call, tenant)] if tenant else _tenants(db, call)
    ids = [t.id for t in tenants]
    rows = db.query(Campaign).filter(Campaign.tenant_id.in_(ids)).order_by(Campaign.id.desc()).all() if ids else []
    return {"campaigns": [campaign_dict(db, c) for c in rows]}


def get_campaign(db: Session, call: Call, campaign_id: int) -> dict:
    return campaign_dict(db, _campaign(db, call, campaign_id), full=True)


def list_leads(db: Session, call: Call, campaign_id: int, status: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    c = _campaign(db, call, campaign_id)
    if status is not None and status not in LEAD_STATUSES:
        raise ApiError(422, "validation_error", "Unbekannter Status", [{"loc": ["status"], "msg": "erlaubt: " + ", ".join(LEAD_STATUSES)}])
    if not 1 <= limit <= 1000 or offset < 0:
        raise ApiError(422, "validation_error", "limit 1..1000, offset >= 0")
    q = db.query(Lead).filter(Lead.campaign_id == c.id, Lead.deleted_at.is_(None))
    if status:
        q = q.filter(Lead.status == status)
    total = q.count()
    rows = q.order_by(Lead.id).offset(offset).limit(limit).all()
    return {"total": total, "offset": offset, "limit": limit, "leads": [lead_dict(l) for l in rows]}


def get_stats(db: Session, call: Call, campaign_id: int) -> dict:
    c = _campaign(db, call, campaign_id)
    lead_ids = select(Lead.id).where(Lead.campaign_id == c.id)
    sent = db.scalar(select(func.count(Message.id)).where(Message.lead_id.in_(lead_ids), Message.direction == "out", Message.status == "sent")) or 0
    contacted = db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == c.id, Lead.last_sent_at.isnot(None))) or 0
    statuses = dict(db.execute(select(Lead.status, func.count()).where(Lead.campaign_id == c.id).group_by(Lead.status)).all())
    replies = dict(
        db.execute(
            select(Message.classification, func.count()).where(Message.lead_id.in_(lead_ids), Message.direction == "in").group_by(Message.classification)
        ).all()
    )
    auto = sum(n for cls, n in replies.items() if cls in ("out_of_office", "auto_reply"))
    drafts = db.scalar(select(func.count(Message.id)).where(Message.lead_id.in_(lead_ids), Message.direction == "out", Message.status == "draft")) or 0
    drafts_parked = db.scalar(
        select(func.count(Message.id)).join(Lead).where(Lead.campaign_id == c.id, Message.direction == "out", Message.status == "draft", Lead.status == "waiting")
    ) or 0
    if c.status == "waiting":
        drafts_parked = drafts
    unsub_reasons = dict(
        db.execute(
            select(Lead.unsubscribe_reason, func.count()).where(Lead.campaign_id == c.id, Lead.status == "unsubscribed").group_by(Lead.unsubscribe_reason)
        ).all()
    )
    t = c.tenant
    return {
        "campaign_id": c.id,
        "status": c.status,
        "sent": sent,
        "leads_contacted": contacted,
        "bounces": statuses.get("bounced", 0),
        "unsubscribed": statuses.get("unsubscribed", 0),
        "unsubscribe_reasons": {k or "ohne_angabe": v for k, v in unsub_reasons.items()},
        "replies": sum(replies.values()) - auto,
        "auto_replies": auto,
        "replies_by_class": {k or "unknown": v for k, v in replies.items()},
        "drafts_waiting_for_approval": drafts - drafts_parked,
        "drafts_parked_waiting": drafts_parked,
        "leads_by_status": statuses,
        "replies_source": {"zammad_configured": bool(t.zammad_url and t.zammad_token_enc), "last_poll": _iso(t.zammad_last_poll)},
    }


# ---------------------------------------------------------------- campaigns
def create_campaign(db: Session, call: Call, data: schemas.CampaignCreate) -> dict:
    tenant = _tenant(db, call, data.tenant)
    values = {
        "tenant": tenant.slug, "name": data.name, "sender_name": data.sender_name, "sender_email": data.sender_email,
        "reply_to": data.reply_to, "language": data.language, "auto_approve": data.auto_approve, "status": "draft",
        "signature_override": data.signature_override or None, "unsubscribe_text_override": data.unsubscribe_text_override or None,
    }
    if not data.confirm:
        return _preview({"would_create": values})
    c = Campaign(
        tenant_id=tenant.id, name=data.name, from_name=data.sender_name, from_email=data.sender_email,
        reply_to=data.reply_to, language=data.language, auto_approve=data.auto_approve, status="draft",
        signature_override=data.signature_override or None, unsubscribe_text_override=data.unsubscribe_text_override or None,
        # leads come from import_leads, not from Google Places
        sourcing_done=True, steps=[],
    )
    db.add(c)
    db.flush()
    call.ids["campaign_id"] = c.id
    pipeline.log_event(db, "api_campaign_created", c.name, tenant.id)
    return {"confirmed": True, "campaign": campaign_dict(db, c, full=True)}


def _sending_problems(c: Campaign) -> list[str]:
    s = get_settings()
    problems = []
    if not c.steps:
        problems.append("Keine Sequenz gesetzt (set_sequence)")
    if not c.tenant.active:
        problems.append("Mandant ist inaktiv")
    if not (c.tenant.smtp2go_api_key_enc or s.smtp2go_api_key):
        problems.append("Kein SMTP2GO-Schlüssel für den Mandanten konfiguriert")
    if any(not templating.is_template_step(st) for st in c.steps or []) and not s.anthropic_api_key:
        problems.append("KI-Schritte ohne ANTHROPIC_API_KEY")
    return problems


def start_campaign(db: Session, call: Call, campaign_id: int, data: schemas.StartCampaign) -> dict:
    c = _campaign(db, call, campaign_id)
    counts = _lead_counts(db, c.id)
    problems = _sending_problems(c)
    t = c.tenant
    preview = {
        "campaign": campaign_dict(db, c),
        "status": {"from": c.status, "to": "active"},
        **({"wait_reason_cleared": c.wait_reason} if c.status == "waiting" else {}),
        "steps": len(c.steps or []),
        "leads_that_will_be_mailed": sum(n for s, n in counts.items() if s in ACTIVE_LEAD_STATUSES),
        "leads_by_status": counts,
        "approval": "automatisch (auto_approve)" if c.auto_approve else "Entwürfe warten auf Freigabe in der Oberfläche",
        "send_window": f"{t.send_hour_start}-{t.send_hour_end} Uhr {t.timezone}" + (", Mo-Fr" if t.send_weekdays_only else ""),
        "daily_limit": t.daily_limit,
        "problems": problems,
    }
    if not data.confirm:
        r = _preview(preview)
        r["hint"] = "Nichts geändert. Starten mit confirm=true und confirm_send=true."
        return r
    if not data.confirm_send:
        raise ApiError(400, "confirm_send_required", "start_campaign löst Versand aus und verlangt zusätzlich confirm_send=true")
    if problems:
        raise ApiError(409, "not_ready", "Kampagne kann nicht gestartet werden", problems)
    old = c.status
    old_reason = c.wait_reason
    c.status = "active"
    c.wait_reason = None
    released = []
    if old == "waiting" and c.auto_approve:
        # drafts written before the campaign was parked: auto_approve applies now
        released = _approve(db, _drafts(db, c))
    pipeline.log_event(db, "campaign_status", f"{c.name} -> active (API)" + (f", war wartend: {old_reason}" if old == "waiting" else ""), c.tenant_id)
    call.detail = f"{old} -> active" + (f"; {len(released)} Entwürfe freigegeben (auto_approve)" if released else "")
    return {"confirmed": True, "campaign": campaign_dict(db, c), "previous_status": old, "drafts_released": len(released)}


def _set_status(db: Session, call: Call, campaign_id: int, data: schemas.Confirm, new: str) -> dict:
    c = _campaign(db, call, campaign_id)
    counts = _lead_counts(db, c.id)
    preview = {
        "campaign": campaign_dict(db, c),
        "status": {"from": c.status, "to": new},
        "leads_pending": sum(n for s, n in counts.items() if s in ACTIVE_LEAD_STATUSES),
        "effect": "Kein Versand und keine neuen Entwürfe, bis die Kampagne wieder gestartet wird",
    }
    if not data.confirm:
        return _preview(preview)
    old = c.status
    c.status = new
    c.wait_reason = None
    pipeline.log_event(db, "campaign_status", f"{c.name} -> {new} (API)", c.tenant_id)
    call.detail = f"{old} -> {new}"
    return {"confirmed": True, "campaign": campaign_dict(db, c), "previous_status": old}


def pause_campaign(db: Session, call: Call, campaign_id: int, data: schemas.Confirm) -> dict:
    return _set_status(db, call, campaign_id, data, "paused")


def stop_campaign(db: Session, call: Call, campaign_id: int, data: schemas.Confirm) -> dict:
    return _set_status(db, call, campaign_id, data, "done")


def wait_campaign(db: Session, call: Call, campaign_id: int, data: schemas.WaitCampaign) -> dict:
    """Park a campaign until a condition is met. Released with start_campaign."""
    c = _campaign(db, call, campaign_id)
    if c.status == "done":
        raise ApiError(409, "campaign_done", "Abgeschlossene Kampagnen können nicht auf wartend gesetzt werden")
    counts = _lead_counts(db, c.id)
    hidden = len(_drafts(db, c, include_waiting=True))
    preview = {
        "campaign": campaign_dict(db, c),
        "status": {"from": c.status, "to": "waiting"},
        "wait_reason": {"old": c.wait_reason, "new": data.reason},
        "leads_pending": sum(n for s, n in counts.items() if s in ACTIVE_LEAD_STATUSES),
        "drafts_hidden_from_review": hidden,
        "effect": "Kein Sourcing, keine neuen Entwürfe, kein Versand. Entwürfe verschwinden aus der Freigabe und können nicht freigegeben werden. Wieder aktiv mit start_campaign.",
    }
    if not data.confirm:
        return _preview(preview)
    old = c.status
    c.status = "waiting"
    c.wait_reason = data.reason
    pipeline.log_event(db, "campaign_status", f"{c.name} -> waiting: {data.reason} (API)", c.tenant_id)
    call.detail = f"{old} -> waiting: {data.reason}"[:2000]
    return {"confirmed": True, "campaign": campaign_dict(db, c), "previous_status": old, "drafts_hidden_from_review": hidden}


def _selected_leads(db: Session, c: Campaign, data) -> list[Lead]:
    q = db.query(Lead).filter(Lead.campaign_id == c.id, Lead.deleted_at.is_(None))
    if data.lead_ids:
        lead_ids = list(dict.fromkeys(data.lead_ids))
        rows = q.filter(Lead.id.in_(lead_ids)).all()
        if len(rows) != len(lead_ids):
            # same answer for foreign, deleted and unknown leads
            raise not_found("Lead")
        return sorted(rows, key=lambda l: l.id)
    return q.order_by(Lead.id).all()


def wait_leads(db: Session, call: Call, campaign_id: int, data: schemas.WaitLeads) -> dict:
    """Park leads until a condition is met: nothing is drafted or sent for them, their
    drafts are hidden from the review queue. Released with release_leads."""
    c = _campaign(db, call, campaign_id)
    leads = _selected_leads(db, c, data)
    eligible = [l for l in leads if l.status in pipeline.WAITABLE_LEAD_STATUSES or l.status == "waiting"]
    skipped = [{"lead_id": l.id, "status": l.status} for l in leads if l not in eligible]
    if data.lead_ids is None:
        skipped = []  # all=true: only report what changes
    preview = {
        "campaign_id": c.id,
        "reason": data.reason,
        "would_park": [{"lead_id": l.id, "firma": l.company, "status": l.status} for l in eligible],
        "not_parkable": skipped,
    }
    if not data.confirm:
        return _preview(preview)
    for l in eligible:
        pipeline.set_lead_waiting(db, l, data.reason)
    call.ids["lead_ids"] = [l.id for l in eligible]
    call.detail = f"{len(eligible)} wartend: {data.reason}"[:2000]
    return {"confirmed": True, "parked": [l.id for l in eligible], "not_parkable": skipped}


def release_leads(db: Session, call: Call, campaign_id: int, data: schemas.ReleaseLeads) -> dict:
    """Put parked leads back into their previous status. Drafts still need approval
    (auto_approve=false); already approved mails go out in the next send window."""
    c = _campaign(db, call, campaign_id)
    leads = [l for l in _selected_leads(db, c, data) if l.status == "waiting"]
    preview = {
        "campaign_id": c.id,
        "would_release": [{"lead_id": l.id, "firma": l.company, "wait_reason": l.wait_reason, "status_after": l.wait_prev_status} for l in leads],
        "approved_mails_sent_next_window": sum(
            1 for l in leads for m in l.messages if m.direction == "out" and m.status == "approved"
        ) if c.status == "active" else 0,
        "note": "Entwürfe brauchen weiterhin die Freigabe, sofern auto_approve=false" + ("; Kampagne ist selbst wartend, Versand erst nach start_campaign" if c.status == "waiting" else ""),
    }
    if not data.confirm:
        return _preview(preview)
    done = {l.id: pipeline.release_lead(db, l) for l in leads}
    call.ids["lead_ids"] = list(done)
    call.detail = f"{len(done)} nicht mehr wartend"
    return {"confirmed": True, "released": [{"lead_id": k, "status": v} for k, v in done.items()]}


def set_sequence(db: Session, call: Call, campaign_id: int, data: schemas.SequenceSet) -> dict:
    c = _campaign(db, call, campaign_id)
    steps = [{"delay_days": s.delay_days, "subject": s.subject, "body_text": s.body_text} for s in data.steps]
    pending = (
        db.query(Message)
        .join(Lead)
        .filter(Lead.campaign_id == c.id, Message.direction == "out", Message.status.in_(("draft", "approved")))
        .count()
    )
    preview = {
        "old_steps": steps_out(c.steps),
        "new_steps": steps_out(steps),
        "placeholders": sorted({p for s in steps for p in templating.placeholders_in(s["subject"] + "\n" + s["body_text"])}),
        "pending_mails_discarded": pending,
    }
    overrides = {k: (v or None) for k, v in (("signature_override", data.signature_override), ("unsubscribe_text_override", data.unsubscribe_text_override)) if v is not None}
    if overrides:
        preview["overrides"] = {k: {"old": getattr(c, k), "new": v} for k, v in overrides.items()}
    if not data.confirm:
        return _preview(preview)
    for k, v in overrides.items():
        setattr(c, k, v)
    c.steps = steps
    discarded = 0
    for lead in db.query(Lead).filter(Lead.campaign_id == c.id).all():
        discarded += pipeline.discard_pending(lead, "Sequenz geändert (API)")
    pipeline.log_event(db, "api_sequence_set", f"{c.name}: {len(steps)} Schritte", c.tenant_id)
    call.detail = f"{len(steps)} Schritte, {discarded} Entwürfe verworfen"
    return {"confirmed": True, "steps": steps_out(steps), "pending_mails_discarded": discarded}


# ---------------------------------------------------------------- leads
def _generic_domain(domain: str) -> bool:
    return domain in pipeline.GENERIC_MAIL_DOMAINS


def import_leads(db: Session, call: Call, campaign_id: int, data: schemas.LeadImport) -> dict:
    c = _campaign(db, call, campaign_id)
    emails = [l.email for l in data.leads]
    existing = dict(
        db.execute(select(func.lower(Lead.email), Lead.id).where(Lead.tenant_id == c.tenant_id, func.lower(Lead.email).in_(set(emails)))).all()
    )
    keys = set(emails) | {"@" + e.rsplit("@", 1)[1] for e in emails}
    suppressed = set(db.scalars(select(Suppression.value).where(Suppression.tenant_id == c.tenant_id, Suppression.value.in_(keys))).all())

    accepted: list[tuple[int, schemas.LeadIn]] = []
    duplicates = []
    seen: dict[str, int] = {}
    for i, item in enumerate(data.leads):
        e = item.email
        if e in seen:
            duplicates.append({"index": i, "email": e, "reason": "duplicate_in_request", "first_index": seen[e]})
        elif e in existing:
            duplicates.append({"index": i, "email": e, "reason": "exists", "lead_id": existing[e]})
        elif e in suppressed or "@" + e.rsplit("@", 1)[1] in suppressed:
            duplicates.append({"index": i, "email": e, "reason": "suppressed"})
        else:
            accepted.append((i, item))
        seen.setdefault(e, i)

    summary = {"received": len(data.leads), "to_create": len(accepted), "skipped": duplicates}
    if not data.confirm:
        summary["to_create_emails"] = [item.email for _, item in accepted]
        return _preview(summary)

    created = []
    for i, item in accepted:
        domain = item.email.rsplit("@", 1)[1]
        lead = Lead(
            tenant_id=c.tenant_id, campaign_id=c.id, company=item.firma, salutation=item.anrede,
            first_name=item.vorname, last_name=item.nachname,
            contact_name=" ".join(p for p in (item.vorname, item.nachname) if p) or None,
            email=item.email, email_source="import", city=item.ort, notes=item.notiz or "", language=item.sprache,
            domain=None if _generic_domain(domain) else domain, status="new",
        )
        db.add(lead)
        db.flush()
        created.append({"index": i, "lead_id": lead.id, "email": lead.email})
    pipeline.log_event(db, "api_import", f"{c.name}: {len(created)} importiert, {len(duplicates)} übersprungen", c.tenant_id)
    call.ids["lead_ids"] = [x["lead_id"] for x in created]
    call.detail = f"{len(created)} angelegt, {len(duplicates)} übersprungen"
    return {"confirmed": True, "received": len(data.leads), "created": created, "skipped": duplicates}


def update_lead(db: Session, call: Call, lead_id: int, data: schemas.LeadUpdate) -> dict:
    lead = _lead(db, call, lead_id)
    changes = {k: v for k, v in data.changes().items() if (getattr(lead, LEAD_FIELDS[k]) or None) != (v or None)}
    if "firma" in changes and not changes["firma"]:
        raise ApiError(422, "validation_error", "firma darf nicht leer sein")
    if "email" in changes:
        new = changes["email"]
        if new is None:
            raise ApiError(422, "validation_error", "email darf nicht leer sein")
        if lead.last_sent_at is not None:
            raise ApiError(409, "already_contacted", "Die Adresse eines bereits angeschriebenen Leads kann nicht geändert werden")
        dup = db.scalar(select(Lead.id).where(Lead.tenant_id == lead.tenant_id, func.lower(Lead.email) == new, Lead.id != lead.id))
        if dup:
            raise ApiError(409, "duplicate", "Adresse existiert bereits beim Mandanten", {"lead_id": dup})
        if pipeline.is_suppressed(db, lead.tenant_id, new):
            raise ApiError(409, "suppressed", "Adresse steht auf der Sperrliste")
    pending = [m.id for m in lead.messages if m.direction == "out" and m.status in ("draft", "approved")]
    preview = {
        "lead_id": lead.id,
        "changes": {k: {"old": getattr(lead, LEAD_FIELDS[k]), "new": v} for k, v in changes.items()},
        "pending_mails_discarded": len(pending) if changes else 0,
        "email_reverified": "email" in changes and lead.status in ACTIVE_LEAD_STATUSES + ("no_email",),
    }
    if not data.confirm or not changes:
        if not changes:
            return {"confirmed": bool(data.confirm), "unchanged": True, "lead": lead_dict(lead)}
        return _preview(preview)
    for k, v in changes.items():
        setattr(lead, LEAD_FIELDS[k], v if k != "notiz" else (v or ""))
    if "vorname" in changes or "nachname" in changes:
        lead.contact_name = " ".join(p for p in (lead.first_name, lead.last_name) if p) or None
    pipeline.discard_pending(lead, "Lead geändert (API)")
    pipeline.reset_generate_failures(db, lead, "Lead geändert (API): " + ", ".join(changes))
    if preview["email_reverified"]:
        domain = lead.email.rsplit("@", 1)[1]
        lead.domain = None if _generic_domain(domain) else domain
        lead.email_source = "import"
        lead.email_mx_ok = None
        if lead.status == "waiting":
            lead.wait_prev_status = "new"  # stays parked, re-verified after release
        else:
            lead.status = "new"
        lead.next_send_at = None
    call.detail = "geändert: " + ", ".join(changes)
    return {"confirmed": True, "lead": lead_dict(lead), "pending_mails_discarded": preview["pending_mails_discarded"]}


def delete_lead(db: Session, call: Call, lead_id: int, data: schemas.Confirm) -> dict:
    lead = _lead(db, call, lead_id)
    if not data.confirm:
        return _preview({"would_delete": lead_dict(lead), "effect": "Soft-Delete: ausgeblendet, keine weiteren Mails"})
    pipeline.discard_pending(lead, "Lead gelöscht (API)")
    lead.deleted_at = datetime.now(timezone.utc)
    if lead.status in ACTIVE_LEAD_STATUSES + ("no_email",):
        lead.status = "stopped"
    lead.next_send_at = None
    pipeline.log_event(db, "api_lead_deleted", lead.email or lead.company, lead.tenant_id, lead.id)
    return {"confirmed": True, "lead_id": lead.id, "deleted_at": _iso(lead.deleted_at)}


def unsuppress_lead(db: Session, call: Call, lead_id: int, data: schemas.Confirm) -> dict:
    """Lift a wrong unsubscribe (e.g. a mail scanner opened the link): remove the
    address from the block list and put the lead back into its sequence."""
    from ..services import scheduling

    lead = _lead(db, call, lead_id)
    if lead.status not in ("unsubscribed", "stopped"):
        raise ApiError(409, "not_suppressed", f"Lead hat Status {lead.status}, nur abgemeldete oder gestoppte Leads können zurückgeholt werden")
    if not lead.email:
        raise ApiError(409, "no_email", "Lead hat keine E-Mail-Adresse")
    steps = lead.campaign.steps or []
    rows = db.query(Suppression).filter(Suppression.tenant_id == lead.tenant_id, Suppression.value == lead.email.lower()).all()
    if lead.step >= len(steps):
        new_status, next_at = "finished", None
    elif lead.step == 0:
        new_status, next_at = "enriched", None
    else:
        delay = int(steps[lead.step].get("delay_days", 3))
        next_at = scheduling.schedule_after(lead.campaign.tenant, delay, lead.last_sent_at)
        now = datetime.now(timezone.utc)
        if next_at < now:
            next_at = scheduling.schedule_after(lead.campaign.tenant, 0)
        new_status = "in_sequence"
    effect = {
        "remove_from_block_list": [r.value for r in rows],
        "new_status": new_status,
        "next_send_at": _iso(next_at),
        "note": "Folgemails brauchen weiterhin die Freigabe, sofern auto_approve=false",
    }
    if not data.confirm:
        return _preview({"lead": lead_dict(lead), "effect": effect})
    for r in rows:
        db.delete(r)
    old = f"{lead.status}, {lead.unsubscribe_source or '-'}, {_iso(lead.unsubscribed_at) or '-'}"
    lead.status = new_status
    lead.next_send_at = next_at
    lead.unsubscribed_at = None
    lead.unsubscribe_source = None
    lead.unsubscribe_reason = None
    lead.unsubscribe_comment = None
    pipeline.log_event(db, "api_lead_unsuppressed", f"{lead.email} (vorher {old})", lead.tenant_id, lead.id)
    return {"confirmed": True, "lead_id": lead.id, **effect}


# ---------------------------------------------------------------- mail preview and test
def _step_cfg(c: Campaign, step: int) -> dict:
    steps = c.steps or []
    if not 1 <= step <= len(steps):
        raise ApiError(422, "validation_error", f"Schritt {step} existiert nicht, die Sequenz hat {len(steps)} Schritte")
    return steps[step - 1]


def _render(c: Campaign, lead: Lead, step: int) -> dict:
    cfg = _step_cfg(c, step)
    if templating.is_template_step(cfg):
        subject, body, missing = templating.render_step(cfg, lead)
        mode = "template"
    else:
        msg = next((m for m in lead.messages if m.direction == "out" and m.step == step - 1 and m.status in ("draft", "approved", "sent")), None) if lead.id else None
        if msg is None:
            raise ApiError(409, "not_rendered", "Dieser Schritt wird per KI geschrieben; eine Vorschau gibt es erst, wenn der Entwurf existiert")
        subject, body, missing, mode = msg.subject, msg.body, [], "llm_draft"
    name, email, reply_to = c.sender
    block, sig = signature.render(c, lead)
    return {
        "step": step, "mode": mode, "from": f"{name} <{email}>", "reply_to": reply_to,
        "subject": subject, "body_text": f"{body.strip()}\n\n{block}", "missing_placeholders": missing,
        "signature": {"text": block, "source": sig.body_source, "unsubscribe_text_source": sig.unsubscribe_source},
    }


def preview_email(db: Session, call: Call, campaign_id: int, lead_id: int, step: int = 1) -> dict:
    c = _campaign(db, call, campaign_id)
    lead = _lead(db, call, lead_id, campaign=c)
    mail = _render(c, lead, step)
    mail["to"] = lead.email
    return mail


def _test_domains() -> set[str]:
    return {d.strip().lower().lstrip("@") for d in get_settings().outreach_test_allow_domains.split(",") if d.strip()}


def send_test(db: Session, call: Call, campaign_id: int, data: schemas.SendTest) -> dict:
    c = _campaign(db, call, campaign_id)
    allowed = _test_domains()
    if data.to.rsplit("@", 1)[1] not in allowed:
        raise ApiError(403, "recipient_not_allowed", "Testmails gehen nur an interne Adressen (OUTREACH_TEST_ALLOW_DOMAINS)")
    if data.lead_id is not None:
        lead = _lead(db, call, data.lead_id, campaign=c)
    else:
        # transient, never added to the session
        lead = Lead(email=data.to, unsubscribe_token="test", **SAMPLE_LEAD)
    mail = _render(c, lead, data.step)
    mail["subject"] = "[TEST] " + mail["subject"]
    mail["to"] = data.to
    call.ids["step"] = data.step
    if not data.confirm:
        return _preview({"would_send": mail})
    api_key = crypto.decrypt(c.tenant.smtp2go_api_key_enc) or get_settings().smtp2go_api_key
    if not api_key:
        raise ApiError(409, "smtp_not_configured", "Kein SMTP2GO-Schlüssel für den Mandanten konfiguriert")
    name, email, reply_to = c.sender
    unsubscribe_url = f"{get_settings().public_base_url.rstrip('/')}/u/test"
    try:
        provider_id, _ = smtp2go.send(
            api_key=api_key, sender=f"{name} <{email}>", to=data.to, subject=mail["subject"], text_body=mail["body_text"],
            reply_to=reply_to, unsubscribe_url=unsubscribe_url, unsubscribe_mailto=reply_to, sender_domain=email.rsplit("@", 1)[-1],
        )
    except smtp2go.SendError as exc:
        raise ApiError(502, "send_failed", str(exc).replace(api_key, "***")[:500])
    pipeline.log_event(db, "api_test_sent", f"{c.name} Schritt {data.step} an {data.to}", c.tenant_id)
    call.detail = f"Test Schritt {data.step} an {data.to}"
    return {"confirmed": True, "sent_to": data.to, "provider_id": provider_id, "mail": mail}


# ---------------------------------------------------------------- signatures
def _signature_dict(sig: TenantSignature) -> dict:
    return {"language": sig.language, "body_text": sig.body_text, "unsubscribe_text": sig.unsubscribe_text, "updated_at": _iso(sig.updated_at)}


def list_tenant_signatures(db: Session, call: Call, tenant: str | None = None) -> dict:
    t = _tenant(db, call, tenant)
    default = "\n\n".join(p for p in (t.signature.strip(), t.footer.strip()) if p)
    return {
        "tenant": t.slug,
        "signatures": [_signature_dict(s) for s in t.signatures],
        "default": {"body_text": default, "unsubscribe_text": signature.DEFAULT_UNSUBSCRIBE_TEXT, "note": "Stufe 4, aus den Mandanteneinstellungen"},
        "resolution": "Kampagnen-Override > Sprache des Leads > Kampagnensprache > default",
    }


def set_tenant_signature(db: Session, call: Call, data: schemas.TenantSignatureSet) -> dict:
    t = _tenant(db, call, data.tenant)
    sig = next((s for s in t.signatures if s.language == data.language), None)
    new = {"language": data.language, "body_text": data.body_text, "unsubscribe_text": data.unsubscribe_text}
    call.ids["language"] = data.language
    if not data.confirm:
        return _preview({"tenant": t.slug, "action": "update" if sig else "create", "old": _signature_dict(sig) if sig else None, "new": new})
    if sig is None:
        sig = TenantSignature(tenant_id=t.id, language=data.language)
        db.add(sig)
    sig.body_text = data.body_text
    sig.unsubscribe_text = data.unsubscribe_text
    db.flush()
    pipeline.log_event(db, "api_signature_set", f"{t.slug}: Signatur {data.language}", t.id)
    call.detail = f"Signatur {data.language}"
    return {"confirmed": True, "tenant": t.slug, "signature": _signature_dict(sig)}


# ---------------------------------------------------------------- campaign settings and approval
CAMPAIGN_FIELDS = {"name": "name", "sender_name": "from_name", "sender_email": "from_email", "reply_to": "reply_to", "auto_approve": "auto_approve"}


def _drafts(db: Session, c: Campaign, status: str = "draft", lead_ids: list[int] | None = None, include_waiting: bool = False) -> list[Message]:
    """Unsent mails of a campaign. Without include_waiting, nothing of a waiting campaign
    and nothing of waiting leads (they are parked and must not be approved)."""
    if c.status == "waiting" and not include_waiting:
        return []
    q = (
        db.query(Message)
        .join(Lead)
        .filter(Lead.campaign_id == c.id, Lead.deleted_at.is_(None), Message.direction == "out", Message.status == status)
    )
    if not include_waiting:
        q = q.filter(Lead.status != "waiting")
    if lead_ids is not None:
        q = q.filter(Message.lead_id.in_(lead_ids))
    return q.order_by(Message.id).all()


def _draft_dict(m: Message, c: Campaign) -> dict:
    lead = m.lead
    return {
        "draft_id": m.id, "status": m.status, "step": m.step + 1, "subject": m.subject,
        "body_text": pipeline.compose_body(c, lead, m.body), "created_at": _iso(m.created_at),
        "lead": {"id": lead.id, "firma": lead.company, "email": lead.email, "status": lead.status},
    }


def _approve(db: Session, msgs: list[Message]) -> list[int]:
    done = []
    for m in msgs:
        pipeline.approve_message(db, m)
        done.append(m.id)
    return done


def update_campaign(db: Session, call: Call, campaign_id: int, data: schemas.CampaignUpdate) -> dict:
    c = _campaign(db, call, campaign_id)
    changes = {k: v for k, v in data.changes().items() if getattr(c, CAMPAIGN_FIELDS[k]) != v}
    drafts = _drafts(db, c) if changes.get("auto_approve") is True else []
    preview = {
        "campaign_id": c.id,
        "changes": {k: {"old": getattr(c, CAMPAIGN_FIELDS[k]), "new": v} for k, v in changes.items()},
        "drafts_released": [{"draft_id": m.id, "lead_id": m.lead_id, "step": m.step + 1} for m in drafts],
    }
    if not changes:
        return {"confirmed": bool(data.confirm), "unchanged": True, "campaign": campaign_dict(db, c, full=True)}
    if not data.confirm:
        return _preview(preview)
    for k, v in changes.items():
        setattr(c, CAMPAIGN_FIELDS[k], v)
    released = _approve(db, drafts)
    pipeline.log_event(db, "api_campaign_updated", f"{c.name}: " + ", ".join(changes), c.tenant_id)
    call.ids["draft_ids"] = released
    call.detail = "geändert: " + ", ".join(changes) + (f"; {len(released)} Entwürfe freigegeben" if released else "")
    return {"confirmed": True, "campaign": campaign_dict(db, c, full=True), "drafts_released": len(released)}


def list_drafts(db: Session, call: Call, campaign_id: int, status: str = "draft", include_waiting: bool = False) -> dict:
    c = _campaign(db, call, campaign_id)
    if status not in ("draft", "approved"):
        raise ApiError(422, "validation_error", "status: draft oder approved")
    drafts = _drafts(db, c, status, include_waiting=include_waiting)
    parked = len(_drafts(db, c, status, include_waiting=True)) - len(_drafts(db, c, status))
    return {"campaign_id": c.id, "status": status, "total": len(drafts), "parked_waiting": parked, "drafts": [_draft_dict(m, c) for m in drafts]}


def approve_drafts(db: Session, call: Call, campaign_id: int, data: schemas.ApproveDrafts) -> dict:
    c = _campaign(db, call, campaign_id)
    if c.status == "waiting":
        raise ApiError(409, "campaign_waiting", f"Kampagne ist wartend ({c.wait_reason or 'ohne Grund'}), zuerst mit start_campaign freigeben")
    lead_ids = None
    if data.lead_ids:
        lead_ids = list(dict.fromkeys(data.lead_ids))
        found = set(db.scalars(select(Lead.id).where(Lead.id.in_(lead_ids), Lead.campaign_id == c.id, Lead.deleted_at.is_(None))).all())
        if len(found) != len(lead_ids):
            # same answer for foreign, deleted and unknown leads
            raise not_found("Lead")
    drafts = _drafts(db, c, "draft", lead_ids)
    waiting = sorted({m.lead_id for m in _drafts(db, c, "draft", lead_ids, include_waiting=True)} - {m.lead_id for m in drafts})
    without = sorted(set(lead_ids or []) - {m.lead_id for m in drafts} - set(waiting))
    preview = {
        "campaign_id": c.id,
        "would_approve": [{"draft_id": m.id, "lead_id": m.lead_id, "email": m.lead.email, "step": m.step + 1, "subject": m.subject} for m in drafts],
        "leads_without_draft": without,
        "leads_waiting": waiting,
    }
    if not data.confirm:
        return _preview(preview)
    approved = _approve(db, drafts)
    pipeline.log_event(db, "api_drafts_approved", f"{c.name}: {len(approved)} Entwürfe", c.tenant_id)
    call.ids["draft_ids"] = approved
    call.ids["lead_ids"] = [m.lead_id for m in drafts]
    call.detail = f"{len(approved)} freigegeben" + (f", {len(waiting)} wartend übersprungen" if waiting else "")
    return {"confirmed": True, "approved": approved, "leads_without_draft": without, "leads_waiting": waiting}


def reject_draft(db: Session, call: Call, draft_id: int, data: schemas.Confirm) -> dict:
    m = db.get(Message, draft_id)
    if m is None or m.direction != "out" or m.lead.deleted_at is not None or not call.principal.may_access(m.lead.campaign.tenant.slug):
        raise not_found("Entwurf")
    call.tenant_id = m.tenant_id
    call.ids.update(draft_id=m.id, lead_id=m.lead_id, campaign_id=m.lead.campaign_id)
    if m.status != "draft":
        raise ApiError(409, "not_a_draft", f"Nachricht hat Status {m.status}, nur Entwürfe können verworfen werden")
    preview = {"draft": _draft_dict(m, m.lead.campaign), "effect": "Entwurf verworfen, Lead auf skipped (keine weiteren Mails)"}
    if not data.confirm:
        return _preview(preview)
    m.status = "failed"
    m.error = "verworfen (API)"
    m.lead.status = "skipped"
    m.lead.next_send_at = None
    pipeline.log_event(db, "api_draft_rejected", f"Entwurf {m.id}", m.tenant_id, m.lead_id)
    return {"confirmed": True, "draft_id": m.id, "lead_id": m.lead_id, "lead_status": "skipped"}


# ---------------------------------------------------------------- audit log and delivery
def list_audit_log(db: Session, call: Call, tenant: str | None = None, campaign_id: int | None = None, tool: str | None = None,
                   since: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Write actions from API, MCP, web UI and the old /api, newest first, for the token's tenants."""
    if not 1 <= limit <= 500 or offset < 0:
        raise ApiError(422, "validation_error", "limit 1..500, offset >= 0")
    tenants = [_tenant(db, call, tenant)] if tenant else _tenants(db, call)
    slugs = {t.id: t.slug for t in tenants}
    q = db.query(ApiAudit)
    cond = ApiAudit.tenant_id.in_(list(slugs))
    if call.principal.tenants is None and not tenant:
        cond = cond | ApiAudit.tenant_id.is_(None)
    q = q.filter(cond)
    if campaign_id is not None:
        _campaign(db, call, campaign_id)
    if tool:
        q = q.filter(ApiAudit.tool == tool)
    if since:
        try:
            q = q.filter(ApiAudit.created_at >= datetime.fromisoformat(since.replace("Z", "+00:00")))
        except ValueError:
            raise ApiError(422, "validation_error", "since als ISO-Datum, z.B. 2026-10-20 oder 2026-10-20T07:00:00Z")
    rows = q.order_by(ApiAudit.id.desc()).limit(5000).all()
    if campaign_id is not None:
        # object_ids is JSON; filtered here so it works the same on Postgres and SQLite
        rows = [r for r in rows if (r.object_ids or {}).get("campaign_id") == campaign_id]
    page = rows[offset: offset + limit]
    return {
        "total": len(rows),
        "entries": [
            {"id": r.id, "created_at": _iso(r.created_at), "actor": r.token_id, "transport": r.transport, "tool": r.tool,
             "tenant": slugs.get(r.tenant_id), "outcome": r.outcome, "object_ids": r.object_ids, "detail": r.detail, "client_ip": r.client_ip}
            for r in page
        ],
        "note": "Einträge aus der Weboberfläche und der alten /api gibt es erst ab dem Update, das sie protokolliert.",
    }


# delivery state from SMTP2GO events, most decisive first
DELIVERY_ORDER = ("hard-bounced", "rejected", "spam", "delivered", "soft-bounced", "processed")


def _delivery_of(events: list[dict]) -> dict:
    kinds = {e.get("event") for e in events}
    state = next((k for k in DELIVERY_ORDER if k in kinds), "unknown")
    decisive = next((e for e in sorted(events, key=lambda e: e.get("date") or "", reverse=True) if e.get("event") == state), {})
    out = {"status": state, "opened": "opened" in kinds, "clicked": "clicked" in kinds, "unsubscribed": "unsubscribed" in kinds,
           "events": sorted(f"{e.get('date')} {e.get('event')}" for e in events)}
    if state in ("hard-bounced", "rejected", "spam", "soft-bounced"):
        out["reason"] = (decisive.get("smtp_response") or decisive.get("reason") or decisive.get("error") or "")[:300]
    return out


def get_delivery_status(db: Session, call: Call, campaign_id: int) -> dict:
    """Per sent mail of the campaign: what SMTP2GO reports (delivered, bounced, ...)."""
    c = _campaign(db, call, campaign_id)
    msgs = (
        db.query(Message).join(Lead)
        .filter(Lead.campaign_id == c.id, Message.direction == "out", Message.status == "sent")
        .order_by(Message.sent_at).all()
    )
    if not msgs:
        return {"campaign_id": c.id, "sent": 0, "by_status": {}, "mails": []}
    api_key = crypto.decrypt(c.tenant.smtp2go_api_key_enc) or get_settings().smtp2go_api_key
    if not api_key:
        raise ApiError(409, "smtp_not_configured", "Kein SMTP2GO-Schlüssel für den Mandanten konfiguriert")
    start = min(m.sent_at for m in msgs if m.sent_at).astimezone(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    by_id: dict[str, list[dict]] = {}
    try:
        for e in smtp2go.activity_search(api_key, start, sender=c.sender[1]):
            by_id.setdefault(str(e.get("email_id")), []).append(e)
        # mails sent before a sender change: look them up one by one (few)
        missing = [m for m in msgs if m.provider_id and m.provider_id not in by_id][:20]
        for m in missing:
            for e in smtp2go.activity_search(api_key, start, email_id=m.provider_id, max_pages=1):
                by_id.setdefault(str(e.get("email_id")), []).append(e)
    except smtp2go.ActivityError as exc:
        raise ApiError(502, "smtp2go_error", str(exc).replace(api_key, "***")[:300])
    mails, counts = [], {}
    for m in msgs:
        d = _delivery_of(by_id.get(m.provider_id or "", []))
        counts[d["status"]] = counts.get(d["status"], 0) + 1
        mails.append({"draft_id": m.id, "lead_id": m.lead_id, "email": m.lead.email, "firma": m.lead.company, "step": m.step + 1,
                      "approved_at": _iso(m.approved_at), "sent_at": _iso(m.sent_at), "provider_id": m.provider_id, **d})
    return {"campaign_id": c.id, "sent": len(msgs), "by_status": counts, "mails": mails,
            "source": "SMTP2GO activity/search; 'unknown' = kein Ereignis gefunden (z.B. älter als die Aufbewahrung bei SMTP2GO)"}
