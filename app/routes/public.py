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


@router.post("/webhooks/smtp2go/{tenant_slug}/{token}")
async def smtp2go_webhook(tenant_slug: str, token: str, request: Request, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.slug == tenant_slug).one_or_none()
    if tenant is None or not hmac.compare_digest(token, tenant.webhook_token):
        raise HTTPException(404)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        payload = await request.json()
    else:
        form = await request.form()
        payload = {k: v for k, v in form.items()}
    events = payload if isinstance(payload, list) else [payload]
    results = [pipeline.handle_webhook(db, tenant, e) for e in events if isinstance(e, dict)]
    db.commit()
    return {"handled": len(results)}
