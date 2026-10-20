"""Unauthenticated endpoints: health, unsubscribe, SMTP2GO webhooks."""

from __future__ import annotations

import hmac
import html

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import __version__, pipeline
from ..db import get_db
from ..models import Lead, Tenant, utcnow

router = APIRouter()

# Reasons offered on the unsubscribe page (key -> label per language)
UNSUB_REASONS: dict[str, dict[str, str]] = {
    "kein_bedarf": {"de": "Kein Bedarf", "fr": "Pas de besoin"},
    "bestehender_lieferant": {"de": "Wir haben bereits einen Lieferanten für solche Produkte", "fr": "Nous avons déjà un fournisseur pour ce type de produits"},
    "falsche_person": {"de": "Ich bin nicht die richtige Ansprechperson", "fr": "Je ne suis pas la bonne personne de contact"},
    "konditionen": {"de": "Preis oder Konditionen passen nicht", "fr": "Le prix ou les conditions ne conviennent pas"},
    "keine_werbung": {"de": "Ich möchte generell keine solchen E-Mails", "fr": "Je ne souhaite pas recevoir ce type d'e-mails"},
    "anderes": {"de": "Anderer Grund", "fr": "Autre raison"},
}

TEXTS = {
    "de": {
        "title": "Abmelden",
        "intro": "Mit einem Klick auf den Button erhalten Sie von uns keine weiteren E-Mails.",
        "question": "Wenn Sie mögen, verraten Sie uns kurz den Grund (freiwillig):",
        "comment": "Bemerkung (freiwillig)",
        "button": "Abmelden",
        "done_title": "Abgemeldet",
        "done": "Sie erhalten von uns keine weiteren E-Mails.",
        "feedback_q": "Wenn Sie mögen, verraten Sie uns kurz den Grund (freiwillig):",
        "feedback_btn": "Rückmeldung senden",
        "thanks": "Vielen Dank für Ihre Rückmeldung.",
    },
    "fr": {
        "title": "Désinscription",
        "intro": "En cliquant sur le bouton, vous ne recevrez plus d'e-mails de notre part.",
        "question": "Si vous le souhaitez, indiquez-nous brièvement la raison (facultatif) :",
        "comment": "Remarque (facultatif)",
        "button": "Se désinscrire",
        "done_title": "Désinscription effectuée",
        "done": "Vous ne recevrez plus d'e-mails de notre part.",
        "feedback_q": "Si vous le souhaitez, indiquez-nous brièvement la raison (facultatif) :",
        "feedback_btn": "Envoyer",
        "thanks": "Merci beaucoup pour votre retour.",
    },
}

PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<style>body{{font-family:Arial,sans-serif;max-width:560px;margin:60px auto;padding:0 20px;color:#222;line-height:1.45}}
label{{display:block;margin:6px 0}}textarea{{width:100%;box-sizing:border-box;margin-top:4px}}
button{{margin-top:14px;padding:10px 18px;font-size:15px;cursor:pointer}}</style></head>
<body><h2>{title}</h2>{body}</body></html>"""

MAX_COMMENT = 1000


def _lang(lead: Lead) -> str:
    lang = (lead.language or getattr(lead.campaign, "language", None) or "de").lower()[:2]
    return lang if lang in TEXTS else "de"


def _form(action: str, lang: str, question: str, button: str) -> str:
    t = TEXTS[lang]
    radios = "".join(
        f'<label><input type="radio" name="reason" value="{key}"> {html.escape(labels[lang])}</label>'
        for key, labels in UNSUB_REASONS.items()
    )
    return (
        f'<form method="post" action="{action}"><p>{question}</p>{radios}'
        f'<label>{t["comment"]}<textarea name="comment" rows="3" maxlength="{MAX_COMMENT}"></textarea></label>'
        f'<button type="submit">{button}</button></form>'
    )


def _page(lang: str, title: str, body: str) -> str:
    return PAGE.format(lang=lang, title=html.escape(title), body=body)


def _save_reason(db: Session, lead: Lead, reason: str | None, comment: str | None) -> bool:
    reason = reason if reason in UNSUB_REASONS else None
    comment = (comment or "").strip()[:MAX_COMMENT] or None
    if not reason and not comment:
        return False
    lead.unsubscribe_reason = reason or lead.unsubscribe_reason
    lead.unsubscribe_comment = comment or lead.unsubscribe_comment
    pipeline.log_event(db, "unsubscribe_reason", f"{lead.email}: {reason or '-'}{' / ' + comment if comment else ''}", lead.tenant_id, lead.id)
    return True


def _unsubscribe(db: Session, lead: Lead, source: str) -> None:
    if lead.email:
        pipeline.suppress(db, lead.tenant_id, lead.email, "unsubscribe")
    if lead.unsubscribed_at is None:
        lead.unsubscribed_at = utcnow()
    pipeline.log_event(db, "unsubscribe", f"{lead.email or '-'} via {source}", lead.tenant_id, lead.id)


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
    """Shows the confirmation page. A GET never unsubscribes: mail security
    scanners open every link in a mail, which would unsubscribe recipients who
    never clicked. The actual unsubscribe is the POST below."""
    lead = _lead_by_token(db, token)
    lang = _lang(lead)
    t = TEXTS[lang]
    if lead.status == "unsubscribed" or lead.unsubscribed_at:
        body = f"<p>{t['done']}</p>" + _form(f"/u/{token}/grund", lang, t["feedback_q"], t["feedback_btn"])
        return _page(lang, t["done_title"], body)
    body = f"<p>{t['intro']}</p>" + _form(f"/u/{token}/abmelden", lang, t["question"], t["button"])
    return _page(lang, t["title"], body)


@router.post("/u/{token}/abmelden", response_class=HTMLResponse)
async def unsubscribe_confirm(token: str, request: Request, db: Session = Depends(get_db)):
    lead = _lead_by_token(db, token)
    form = await request.form()
    _unsubscribe(db, lead, "page")
    _save_reason(db, lead, form.get("reason"), form.get("comment"))
    db.commit()
    t = TEXTS[_lang(lead)]
    return _page(_lang(lead), t["done_title"], f"<p>{t['done']}</p>")


@router.post("/u/{token}/grund", response_class=HTMLResponse)
async def unsubscribe_reason(token: str, request: Request, db: Session = Depends(get_db)):
    """Reason given afterwards (e.g. after a one-click unsubscribe)."""
    lead = _lead_by_token(db, token)
    form = await request.form()
    _save_reason(db, lead, form.get("reason"), form.get("comment"))
    db.commit()
    t = TEXTS[_lang(lead)]
    return _page(_lang(lead), t["done_title"], f"<p>{t['done']}</p><p>{t['thanks']}</p>")


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe_post(token: str, db: Session = Depends(get_db)):
    # RFC 8058 one-click (List-Unsubscribe-Post) from the mail client: unsubscribe at once
    lead = _lead_by_token(db, token)
    _unsubscribe(db, lead, "one-click")
    db.commit()
    t = TEXTS[_lang(lead)]
    return _page(_lang(lead), t["done_title"], f"<p>{t['done']}</p>")


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
