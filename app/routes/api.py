"""JSON API (Bearer API_TOKEN) for scripts and a future MCP server."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import pipeline
from ..auth import require_api_token
from ..db import get_db
from ..models import Campaign, Lead, Message, Tenant

router = APIRouter(dependencies=[Depends(require_api_token)])


def _lead_dict(l: Lead) -> dict:
    return {
        "id": l.id, "tenant_id": l.tenant_id, "campaign_id": l.campaign_id, "company": l.company,
        "address": l.address, "phone": l.phone, "website": l.website, "email": l.email,
        "email_source": l.email_source, "contact_name": l.contact_name, "status": l.status,
        "step": l.step, "next_send_at": l.next_send_at.isoformat() if l.next_send_at else None,
        "last_sent_at": l.last_sent_at.isoformat() if l.last_sent_at else None, "notes": l.notes,
    }


@router.get("/tenants")
def tenants(db: Session = Depends(get_db)):
    return [{"id": t.id, "slug": t.slug, "name": t.name, "active": t.active, "daily_limit": t.daily_limit} for t in db.query(Tenant).all()]


@router.get("/campaigns")
def campaigns(db: Session = Depends(get_db)):
    return [
        {"id": c.id, "tenant_id": c.tenant_id, "name": c.name, "status": c.status, "auto_approve": c.auto_approve, "sourcing_done": c.sourcing_done}
        for c in db.query(Campaign).all()
    ]


@router.get("/leads")
def leads(campaign_id: int | None = None, status: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    q = db.query(Lead)
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)
    if status:
        q = q.filter(Lead.status == status)
    return [_lead_dict(l) for l in q.order_by(Lead.id.desc()).limit(min(limit, 1000)).all()]


@router.get("/leads/{lead_id}")
def lead(lead_id: int, db: Session = Depends(get_db)):
    l = db.get(Lead, lead_id)
    if not l:
        raise HTTPException(404)
    d = _lead_dict(l)
    d["messages"] = [
        {"id": m.id, "direction": m.direction, "step": m.step, "subject": m.subject, "body": m.body, "status": m.status,
         "sent_at": m.sent_at.isoformat() if m.sent_at else None, "classification": m.classification}
        for m in l.messages
    ]
    return d


class MessageEdit(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.post("/messages/{message_id}/approve")
def approve(message_id: int, edit: MessageEdit | None = None, db: Session = Depends(get_db)):
    m = db.get(Message, message_id)
    if not m or m.direction != "out" or m.status != "draft":
        raise HTTPException(404)
    if edit:
        if edit.subject:
            m.subject = edit.subject[:300]
        if edit.body:
            m.body = edit.body
    pipeline.approve_message(db, m)
    db.commit()
    return {"ok": True, "next_send_at": m.lead.next_send_at.isoformat() if m.lead.next_send_at else None}


@router.post("/leads/{lead_id}/stop")
def stop(lead_id: int, db: Session = Depends(get_db)):
    l = db.get(Lead, lead_id)
    if not l:
        raise HTTPException(404)
    l.status = "stopped"
    l.next_send_at = None
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/source")
def source(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404)
    result = pipeline.run_sourcing(db, c)
    db.commit()
    return {"result": result}
