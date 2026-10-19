"""Unauthenticated endpoints: health, unsubscribe, SMTP2GO webhooks."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import __version__, pipeline
from ..db import get_db
from ..models import Lead, Tenant

router = APIRouter()

UNSUB_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Abmeldung</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:Arial,sans-serif;max-width:560px;margin:60px auto;padding:0 20px;color:#222}}</style></head>
<body><h2>{title}</h2><p>{text}</p></body></html>"""


@router.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


def _lead_by_token(db: Session, token: str) -> Lead:
    lead = db.query(Lead).filter(Lead.unsubscribe_token == token).one_or_none()
    if lead is None:
        raise HTTPException(404)
    return lead


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_get(token: str, db: Session = Depends(get_db)):
    lead = _lead_by_token(db, token)
    if lead.email:
        pipeline.suppress(db, lead.tenant_id, lead.email, "unsubscribe")
        db.commit()
    return UNSUB_PAGE.format(title="Abgemeldet", text="Sie erhalten von uns keine weiteren E-Mails. Danke für die Rückmeldung.")


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe_post(token: str, db: Session = Depends(get_db)):
    # RFC 8058 one-click (List-Unsubscribe-Post)
    return unsubscribe_get(token, db)


@router.post("/webhooks/smtp2go/{token}")
async def smtp2go_webhook_shared(token: str, request: Request, db: Session = Depends(get_db)):
    """One webhook for all tenants: the tenant is found via the SMTP2GO email id,
    the Message-ID header, or the sender address."""
    from ..config import get_settings
    from ..models import Message
    from ..services import smtp2go

    expected = get_settings().webhook_token
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(404)
    payload = await _payload(request)
    results = []
    for e in payload:
        event, rcpt, email_id, message_id = smtp2go.parse_webhook(e)
        tenant = None
        msg = None
        if email_id:
            msg = db.query(Message).filter(Message.provider_id == str(email_id)).first()
        if msg is None and message_id:
            msg = db.query(Message).filter(Message.message_id_header == message_id).first()
        if msg is not None:
            tenant = db.get(Tenant, msg.tenant_id)
        if tenant is None:
            sender = smtp2go.webhook_sender(e)
            if sender:
                tenant = db.query(Tenant).filter(Tenant.from_email == sender).first()
        if tenant is None:
            pipeline.log_event(db, "webhook_ignored", f"{event or '?'} rcpt={rcpt or '-'} sender={smtp2go.webhook_sender(e) or '-'} id={email_id or message_id or '-'}")
            results.append("ignored")
            continue
        results.append(pipeline.handle_webhook(db, tenant, e))
    db.commit()
    return {"handled": sum(1 for r in results if r != "ignored"), "ignored": results.count("ignored")}


async def _payload(request: Request) -> list[dict]:
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        payload = await request.json()
    else:
        form = await request.form()
        payload = {k: v for k, v in form.items()}
    events = payload if isinstance(payload, list) else [payload]
    return [e for e in events if isinstance(e, dict)]


@router.post("/webhooks/smtp2go/{tenant_slug}/{token}")
async def smtp2go_webhook(tenant_slug: str, token: str, request: Request, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.slug == tenant_slug).one_or_none()
    if tenant is None or not hmac.compare_digest(token, tenant.webhook_token):
        raise HTTPException(404)
    results = [pipeline.handle_webhook(db, tenant, e) for e in await _payload(request)]
    db.commit()
    return {"handled": len(results)}
