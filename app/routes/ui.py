"""Server-rendered admin UI."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crypto, pipeline
from ..auth import COOKIE, check_login, create_session, require_csrf, require_login
from ..config import get_settings
from ..db import get_db
from ..models import LEAD_STATUSES, Campaign, Event, Lead, Message, Suppression, Tenant, TenantSignature
from ..services import scheduling, templating

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["lead_statuses"] = LEAD_STATUSES


def render(request: Request, name: str, sess: dict | None = None, **ctx):
    ctx.update({"request": request, "csrf": (sess or {}).get("csrf", ""), "settings": get_settings()})
    return templates.TemplateResponse(request, name, ctx)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if not check_login(username, password, ip):
        return render(request, "login.html", error="Login fehlgeschlagen")
    resp = _redirect("/")
    secure = get_settings().public_base_url.startswith("https://")
    resp.set_cookie(COOKIE, create_session(), httponly=True, samesite="strict", secure=secure, max_age=12 * 3600)
    return resp


@router.post("/logout")
def logout():
    resp = _redirect("/login")
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    rows = []
    for t in tenants:
        counts = dict(db.execute(select(Lead.status, func.count()).where(Lead.tenant_id == t.id).group_by(Lead.status)).all())
        drafts = db.scalar(select(func.count(Message.id)).where(Message.tenant_id == t.id, Message.status == "draft")) or 0
        rows.append(
            {
                "tenant": t,
                "counts": counts,
                "total": sum(counts.values()),
                "drafts": drafts,
                "sent_today": scheduling.sent_today(db, t),
                "in_window": scheduling.in_window(t),
                "has_key": bool(t.smtp2go_api_key_enc or get_settings().smtp2go_api_key),
            }
        )
    events = db.query(Event).order_by(Event.id.desc()).limit(25).all()
    campaigns = db.query(Campaign).order_by(Campaign.id.desc()).all()
    return render(request, "dashboard.html", sess, rows=rows, events=events, campaigns=campaigns)


# ---------------------------------------------------------------- tenants
@router.get("/tenants", response_class=HTMLResponse)
def tenants(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    return render(request, "tenants.html", sess, tenants=db.query(Tenant).order_by(Tenant.name).all())


@router.get("/tenants/new", response_class=HTMLResponse)
def tenant_new(request: Request, sess: dict = Depends(require_login)):
    return render(request, "tenant_form.html", sess, t=None)


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_edit(tenant_id: int, request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404)
    return render(request, "tenant_form.html", sess, t=t)


@router.post("/tenants/save")
async def tenant_save(request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    tid = f.get("id")
    t = db.get(Tenant, int(tid)) if tid else Tenant(slug="", name="", from_name="", from_email="")
    t.slug = f.get("slug", "").strip().lower()[:40]
    t.name = f.get("name", "").strip()[:120]
    t.active = f.get("active") == "on"
    t.from_name = f.get("from_name", "").strip()[:120]
    t.from_email = f.get("from_email", "").strip().lower()[:200]
    t.reply_to = f.get("reply_to", "").strip().lower()[:200] or None
    t.website = f.get("website", "").strip()[:200] or None
    t.company_blurb = f.get("company_blurb", "").strip()
    t.signature = f.get("signature", "").strip()
    t.footer = f.get("footer", "").strip()
    t.daily_limit = int(f.get("daily_limit") or 40)
    t.send_hour_start = int(f.get("send_hour_start") or 9)
    t.send_hour_end = int(f.get("send_hour_end") or 17)
    t.send_weekdays_only = f.get("send_weekdays_only") == "on"
    t.timezone = f.get("timezone", "Europe/Zurich").strip() or "Europe/Zurich"
    if f.get("smtp2go_api_key", "").strip():
        t.smtp2go_api_key_enc = crypto.encrypt(f["smtp2go_api_key"].strip())
    t.zammad_url = f.get("zammad_url", "").strip() or None
    t.zammad_group = f.get("zammad_group", "").strip() or None
    if f.get("zammad_token", "").strip():
        t.zammad_token_enc = crypto.encrypt(f["zammad_token"].strip())
    if not t.slug or not t.name or not t.from_email:
        raise HTTPException(400, "Slug, Name und Absenderadresse sind Pflicht")
    db.add(t)
    db.commit()
    return _redirect(f"/tenants/{t.id}")


@router.post("/tenants/{tenant_id}/signatures")
async def tenant_signature_save(tenant_id: int, request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404)
    lang = f.get("language", "").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", lang):
        raise HTTPException(400, "Sprache als ISO-639-1-Code, z.B. fr")
    sig = db.query(TenantSignature).filter_by(tenant_id=t.id, language=lang).one_or_none()
    if f.get("action") == "delete":
        if sig:
            db.delete(sig)
    else:
        body = f.get("body_text", "").strip()
        unsub = " ".join(f.get("unsubscribe_text", "").split())
        problems = templating.check_template(body + "\n" + unsub, allowed=templating.SIGNATURE_PLACEHOLDERS)
        if not body or not unsub or problems:
            raise HTTPException(400, "; ".join(problems) or "Signatur und Abmeldetext sind Pflicht")
        if sig is None:
            sig = TenantSignature(tenant_id=t.id, language=lang)
            db.add(sig)
        sig.body_text = body
        sig.unsubscribe_text = unsub
    pipeline.log_event(db, "signature", f"{t.slug}: Signatur {lang} {'gelöscht' if f.get('action') == 'delete' else 'gespeichert'}", t.id)
    db.commit()
    return _redirect(f"/tenants/{t.id}")


# ---------------------------------------------------------------- campaigns
@router.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    rows = []
    for c in db.query(Campaign).order_by(Campaign.id.desc()).all():
        counts = dict(db.execute(select(Lead.status, func.count()).where(Lead.campaign_id == c.id).group_by(Lead.status)).all())
        rows.append({"c": c, "counts": counts, "total": sum(counts.values())})
    return render(request, "campaigns.html", sess, rows=rows)


@router.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    if not tenants:
        return _redirect("/tenants/new")
    return render(request, "campaign_form.html", sess, c=None, tenants=tenants, steps_json=json.dumps(DEFAULT_STEPS, ensure_ascii=False, indent=2))


DEFAULT_STEPS = [
    {"delay_days": 0, "instruction": "Erstkontakt: kurz, konkret, mit einem Bezug zur Webseite des Empfängers. Eine klare Frage am Schluss."},
    {"delay_days": 4, "instruction": "Kurzes Follow-up, freundlich nachfassen, maximal 60 Wörter, einen zusätzlichen Nutzen nennen."},
    {"delay_days": 8, "instruction": "Letztes Follow-up: knapp, ohne Druck, Tür offen lassen."},
]


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_edit(campaign_id: int, request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404)
    counts = dict(db.execute(select(Lead.status, func.count()).where(Lead.campaign_id == c.id).group_by(Lead.status)).all())
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return render(request, "campaign_form.html", sess, c=c, tenants=tenants, counts=counts, steps_json=json.dumps(c.steps or DEFAULT_STEPS, ensure_ascii=False, indent=2))


@router.post("/campaigns/save")
async def campaign_save(request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    cid = f.get("id")
    c = db.get(Campaign, int(cid)) if cid else Campaign(tenant_id=int(f["tenant_id"]), name="")
    c.name = f.get("name", "").strip()[:120]
    c.search_queries = f.get("search_queries", "").strip()
    c.target_description = f.get("target_description", "").strip()
    c.offer = f.get("offer", "").strip()
    c.language = f.get("language", "de").strip()[:10]
    c.tone = f.get("tone", "").strip()
    c.auto_approve = f.get("auto_approve") == "on"
    c.max_leads = int(f.get("max_leads") or 200)
    try:
        steps = json.loads(f.get("steps", "[]"))
        assert isinstance(steps, list) and steps
        # fixed-text steps (set via the API) keep subject and body_text
        c.steps = [
            {"delay_days": int(s.get("delay_days", 0)), "instruction": str(s.get("instruction", ""))}
            | ({"subject": str(s.get("subject", "")), "body_text": str(s["body_text"])} if s.get("body_text") else {})
            for s in steps
        ]
    except Exception:
        raise HTTPException(400, "Schritte müssen eine JSON-Liste mit delay_days und instruction sein")
    for field in ("signature_override", "unsubscribe_text_override"):
        value = f.get(field, "").strip()
        problems = templating.check_template(value, allowed=templating.SIGNATURE_PLACEHOLDERS)
        if problems:
            raise HTTPException(400, "; ".join(problems))
        setattr(c, field, value or None)
    if not c.name:
        raise HTTPException(400, "Name fehlt")
    db.add(c)
    db.commit()
    return _redirect(f"/campaigns/{c.id}")


@router.post("/campaigns/{campaign_id}/status")
async def campaign_status(campaign_id: int, request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404)
    status = f.get("status")
    if status in ("draft", "active", "paused", "done"):
        c.status = status
        pipeline.log_event(db, "campaign_status", f"{c.name} -> {status}", c.tenant_id)
    if f.get("action") == "resource":
        c.sourcing_done = False
    db.commit()
    return _redirect(f"/campaigns/{c.id}")


# ---------------------------------------------------------------- leads
@router.get("/leads", response_class=HTMLResponse)
def leads(request: Request, campaign: int | None = None, status: str | None = None, q: str | None = None, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    query = db.query(Lead).filter(Lead.deleted_at.is_(None))
    if campaign:
        query = query.filter(Lead.campaign_id == campaign)
    if status:
        query = query.filter(Lead.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((Lead.company.ilike(like)) | (Lead.email.ilike(like)) | (Lead.domain.ilike(like)))
    rows = query.order_by(Lead.id.desc()).limit(500).all()
    campaigns = db.query(Campaign).order_by(Campaign.id.desc()).all()
    return render(request, "leads.html", sess, leads=rows, campaigns=campaigns, campaign=campaign, status=status, q=q or "")


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    events = db.query(Event).filter(Event.lead_id == lead.id).order_by(Event.id.desc()).limit(30).all()
    return render(request, "lead.html", sess, lead=lead, events=events)


@router.post("/leads/{lead_id}/action")
async def lead_action(lead_id: int, request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    action = f.get("action")
    if action == "stop":
        lead.status = "stopped"
        lead.next_send_at = None
        for m in lead.messages:
            if m.direction == "out" and m.status in ("draft", "approved"):
                m.status = "failed"
                m.error = "manuell gestoppt"
    elif action == "skip":
        lead.status = "skipped"
        lead.next_send_at = None
    elif action == "requeue":
        pipeline.reset_generate_failures(db, lead, "neu anreichern (UI)")
        lead.status = "new"
        lead.email = None
        lead.email_source = None
        lead.email_candidates = None
        lead.contact_name = None
        lead.salutation = None
        lead.last_error = None
        lead.next_send_at = None
        for m in lead.messages:
            if m.direction == "out" and m.status in ("draft", "approved"):
                m.status = "failed"
                m.error = "verworfen, Lead neu angereichert"
    elif action == "set_email":
        email = f.get("email", "").strip().lower()
        if email:
            pipeline.reset_generate_failures(db, lead, "Adresse gesetzt (UI)")
            lead.email = email
            lead.email_source = "manual"
            from ..services import verify

            lead.email_mx_ok = verify.check(email)
            lead.status = "enriched" if lead.email_mx_ok else "no_email"
            lead.notes = "" if lead.email_mx_ok else f"{email}: kein MX-Eintrag"
    elif action == "generate":
        pipeline.reset_generate_failures(db, lead, "Entwurf manuell angestossen (UI)")
        try:
            pipeline.generate_for_lead(db, lead)
        except Exception as exc:
            lead.last_error = f"{type(exc).__name__}: {exc}"[:500]
    elif action == "suppress":
        if lead.email:
            pipeline.suppress(db, lead.tenant_id, lead.email, "manual")
    elif action == "note":
        lead.notes = f.get("notes", "").strip()
    db.commit()
    return _redirect(f"/leads/{lead.id}")


# ---------------------------------------------------------------- review queue
@router.get("/review", response_class=HTMLResponse)
def review(request: Request, tenant: int | None = None, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    q = db.query(Message).join(Lead).filter(Message.direction == "out", Message.status == "draft")
    if tenant:
        q = q.filter(Message.tenant_id == tenant)
    drafts = q.order_by(Message.id).limit(50).all()
    total = db.scalar(select(func.count(Message.id)).where(Message.direction == "out", Message.status == "draft")) or 0
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return render(request, "review.html", sess, drafts=drafts, total=total, tenants=tenants, tenant=tenant)


@router.post("/messages/{message_id}/decide")
async def message_decide(message_id: int, request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    m = db.get(Message, message_id)
    if not m or m.direction != "out":
        raise HTTPException(404)
    decision = f.get("decision")
    back = f.get("back") or "/review"
    if decision == "approve" and m.status == "draft":
        m.subject = f.get("subject", m.subject).strip()[:300]
        m.body = f.get("body", m.body).strip()
        pipeline.approve_message(db, m)
    elif decision == "regenerate" and m.status in ("draft", "approved"):
        m.status = "failed"
        m.error = "verworfen, neu generiert"
        lead = m.lead
        lead.status = "enriched" if lead.step == 0 else "in_sequence"
        try:
            pipeline.generate_for_lead(db, lead)
        except Exception as exc:
            lead.last_error = f"{type(exc).__name__}: {exc}"[:500]
    elif decision == "discard":
        m.status = "failed"
        m.error = "verworfen"
        m.lead.status = "skipped"
        m.lead.next_send_at = None
    elif decision == "save" and m.status == "draft":
        m.subject = f.get("subject", m.subject).strip()[:300]
        m.body = f.get("body", m.body).strip()
    db.commit()
    return _redirect(back)


# ---------------------------------------------------------------- suppressions & events
@router.get("/suppressions", response_class=HTMLResponse)
def suppressions(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    rows = db.query(Suppression).order_by(Suppression.id.desc()).limit(500).all()
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return render(request, "suppressions.html", sess, rows=rows, tenants=tenants)


@router.post("/suppressions/add")
async def suppression_add(request: Request, sess: dict = Depends(require_csrf), db: Session = Depends(get_db)):
    f = await request.form()
    pipeline.suppress(db, int(f["tenant_id"]), f.get("value", ""), "manual")
    db.commit()
    return _redirect("/suppressions")


@router.get("/events", response_class=HTMLResponse)
def events(request: Request, sess: dict = Depends(require_login), db: Session = Depends(get_db)):
    rows = db.query(Event).order_by(Event.id.desc()).limit(300).all()
    return render(request, "events.html", sess, events=rows)
