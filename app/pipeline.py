"""The outreach pipeline. Each function does one bounded unit of work on a DB session and
is called by the worker loop (and by UI actions)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .models import Campaign, Event, Lead, Message, Suppression, Tenant
from .services import crawler, hunter, imap_replies, llm, places, scheduling, smtp2go, verify, zammad

log = logging.getLogger(__name__)


def log_event(db: Session, kind: str, detail: str = "", tenant_id: int | None = None, lead_id: int | None = None) -> None:
    db.add(Event(kind=kind, detail=detail[:4000], tenant_id=tenant_id, lead_id=lead_id))


def is_suppressed(db: Session, tenant_id: int, email: str | None) -> bool:
    if not email:
        return False
    email = email.lower()
    dom = "@" + email.rsplit("@", 1)[1]
    q = select(Suppression.id).where(Suppression.tenant_id == tenant_id, Suppression.value.in_([email, dom]))
    return db.execute(q).first() is not None


def suppress(db: Session, tenant_id: int, value: str, reason: str) -> None:
    value = value.lower().strip()
    if not value:
        return
    exists = db.execute(select(Suppression.id).where(Suppression.tenant_id == tenant_id, Suppression.value == value)).first()
    if not exists:
        db.add(Suppression(tenant_id=tenant_id, value=value, reason=reason))
    # stop every active lead with that address
    for lead in db.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.email == value).all():
        if lead.status in ("needs_review", "scheduled", "in_sequence", "enriched"):
            lead.status = "unsubscribed" if reason == "unsubscribe" else "bounced" if reason in ("bounce", "spam") else "stopped"
            lead.next_send_at = None
            for m in lead.messages:
                if m.direction == "out" and m.status in ("draft", "approved"):
                    m.status = "failed"
                    m.error = f"suppressed: {reason}"


# ---------------------------------------------------------------- sourcing
def run_sourcing(db: Session, campaign: Campaign) -> str:
    """Query Google Places for every search line, create new leads. Idempotent per place."""
    tenant = campaign.tenant
    created = 0
    seen = 0
    lines = []
    existing_count = db.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == campaign.id)) or 0
    budget = max(0, campaign.max_leads - existing_count)
    for query in campaign.query_list:
        if budget <= 0:
            lines.append(f"{query}: Lead-Limit erreicht")
            break
        try:
            hits = places.search_text(query, max_results=min(60, budget * 2))
        except places.PlacesError as exc:
            lines.append(f"{query}: FEHLER {exc}")
            log.warning("sourcing %s: %s", query, exc)
            continue
        new_here = 0
        for hit in hits:
            seen += 1
            dup = db.execute(select(Lead.id).where(Lead.tenant_id == tenant.id, Lead.place_id == hit.place_id)).first()
            if dup:
                continue
            try:
                website, phone = places.details(hit.place_id)
            except places.PlacesError as exc:
                log.warning("details %s: %s", hit.place_id, exc)
                website, phone = None, None
            lead = Lead(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                place_id=hit.place_id,
                company=hit.name[:200],
                address=hit.address,
                phone=phone,
                website=website,
                domain=crawler.domain_of(website),
                place_types=",".join(hit.types)[:300],
                source_query=query[:200],
                status="new",
            )
            # same domain already contacted by this tenant -> skip quietly
            if lead.domain and db.execute(select(Lead.id).where(Lead.tenant_id == tenant.id, Lead.domain == lead.domain)).first():
                lead.status = "skipped"
                lead.notes = "Domain bereits in einer anderen Kampagne"
            db.add(lead)
            created += 1
            new_here += 1
            budget -= 1
            if budget <= 0:
                break
        lines.append(f"{query}: {len(hits)} Treffer, {new_here} neu")
        db.flush()
    campaign.sourcing_done = True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    campaign.sourcing_log = (campaign.sourcing_log + f"\n[{stamp}] " + " | ".join(lines)).strip()[-8000:]
    log_event(db, "sourcing", f"{campaign.name}: {created} neue Leads aus {seen} Treffern", tenant.id)
    return f"{created} neue Leads"


# ---------------------------------------------------------------- enrichment
def enrich_lead(db: Session, lead: Lead) -> None:
    """Find an e-mail address and website excerpts for one lead."""
    if is_suppressed(db, lead.tenant_id, lead.email):
        lead.status = "stopped"
        lead.notes = "Adresse auf Sperrliste"
        return
    if not lead.website:
        lead.status = "no_email"
        lead.notes = "Keine Webseite bei Google hinterlegt"
        return

    res = crawler.crawl(lead.website)
    lead.domain = res.domain or lead.domain
    lead.site_excerpt = res.site_excerpt or None
    lead.impressum_excerpt = res.impressum_excerpt or None
    email = res.best_email
    source = "crawler" if email else None

    if not email and lead.domain:
        best = hunter.pick_best(hunter.domain_search(lead.domain))
        if best and best.get("value"):
            email = best["value"].lower()
            source = "hunter"
            if best.get("first_name") and best.get("last_name"):
                lead.contact_name = f"{best['first_name']} {best['last_name']}"

    if not email:
        lead.status = "no_email"
        lead.notes = (res.error or "Keine E-Mail auf der Webseite gefunden")[:500]
        return

    if is_suppressed(db, lead.tenant_id, email):
        lead.status = "stopped"
        lead.notes = "Adresse auf Sperrliste"
        return

    lead.email = email
    lead.email_source = source
    lead.email_mx_ok = verify.check(email)
    if not lead.email_mx_ok:
        lead.status = "no_email"
        lead.notes = f"{email}: kein MX-Eintrag"
        return
    lead.status = "enriched"
    lead.notes = f"{res.pages_fetched} Seiten geladen; {len(res.emails)} Adressen" + (f"; Fehler: {res.error}" if res.error else "")


def enrich_batch(db: Session, limit: int) -> int:
    leads = (
        db.query(Lead)
        .join(Campaign)
        .filter(Lead.status == "new", Campaign.status == "active")
        .order_by(Lead.id)
        .limit(limit)
        .all()
    )
    for lead in leads:
        try:
            enrich_lead(db, lead)
        except Exception as exc:
            log.exception("enrich lead %s", lead.id)
            lead.status = "no_email"
            lead.last_error = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
    return len(leads)


# ---------------------------------------------------------------- generation
def generate_for_lead(db: Session, lead: Lead) -> Message | None:
    campaign = lead.campaign
    tenant = campaign.tenant
    steps = campaign.steps or [{"delay_days": 0, "instruction": ""}]
    if lead.step >= len(steps):
        lead.status = "finished"
        return None
    # already a draft/approved message for this step?
    for m in lead.messages:
        if m.direction == "out" and m.step == lead.step and m.status in ("draft", "approved"):
            return m
    previous = [(m.subject, m.body) for m in lead.messages if m.direction == "out" and m.status == "sent"]
    draft = llm.write_mail(tenant, campaign, lead, lead.step, previous)
    if draft.contact_name and not lead.contact_name:
        lead.contact_name = draft.contact_name[:120]
    if draft.salutation:
        lead.salutation = draft.salutation[:120]
    msg = Message(lead_id=lead.id, tenant_id=tenant.id, direction="out", step=lead.step, subject=draft.subject, body=draft.body)
    if campaign.auto_approve:
        msg.status = "approved"
        msg.approved_at = datetime.now(timezone.utc)
        lead.status = "scheduled" if lead.step == 0 else "in_sequence"
        if lead.next_send_at is None:
            lead.next_send_at = scheduling.schedule_after(tenant, 0)
    else:
        msg.status = "draft"
        lead.status = "needs_review"
    db.add(msg)
    return msg


def generate_batch(db: Session, limit: int) -> int:
    # first mails for enriched leads
    leads = (
        db.query(Lead)
        .join(Campaign)
        .filter(Lead.status == "enriched", Campaign.status == "active")
        .order_by(Lead.id)
        .limit(limit)
        .all()
    )
    # follow-ups: draft the next step a day ahead so there is time to review it
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    followups = (
        db.query(Lead)
        .join(Campaign)
        .filter(Lead.status == "in_sequence", Campaign.status == "active", Lead.next_send_at <= soon)
        .order_by(Lead.next_send_at)
        .limit(limit)
        .all()
    )
    n = 0
    for lead in leads + followups:
        try:
            generate_for_lead(db, lead)
            n += 1
        except Exception as exc:
            log.exception("generate lead %s", lead.id)
            lead.last_error = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
    return n


# ---------------------------------------------------------------- sending
def approve_message(db: Session, msg: Message) -> None:
    tenant = msg.lead.campaign.tenant
    msg.status = "approved"
    msg.approved_at = datetime.now(timezone.utc)
    lead = msg.lead
    lead.status = "scheduled" if lead.step == 0 else "in_sequence"
    if lead.next_send_at is None or lead.next_send_at < datetime.now(timezone.utc):
        lead.next_send_at = scheduling.schedule_after(tenant, 0)


def send_due(db: Session, limit: int) -> int:
    now = datetime.now(timezone.utc)
    s = get_settings()
    sent = 0
    tenants = db.query(Tenant).filter(Tenant.active.is_(True)).all()
    for tenant in tenants:
        if not scheduling.in_window(tenant):
            continue
        api_key = crypto.decrypt(tenant.smtp2go_api_key_enc) or s.smtp2go_api_key
        if not api_key:
            continue
        remaining = tenant.daily_limit - scheduling.sent_today(db, tenant)
        if remaining <= 0:
            continue
        msgs = (
            db.query(Message)
            .join(Lead)
            .join(Campaign)
            .filter(
                Message.tenant_id == tenant.id,
                Message.direction == "out",
                Message.status == "approved",
                Campaign.status == "active",
                Lead.status.in_(("scheduled", "in_sequence")),
                Lead.next_send_at <= now,
            )
            .order_by(Lead.next_send_at)
            .limit(min(limit, remaining))
            .all()
        )
        for msg in msgs:
            lead = msg.lead
            if msg.step != lead.step:
                continue
            if is_suppressed(db, tenant.id, lead.email):
                lead.status = "stopped"
                msg.status = "failed"
                msg.error = "suppressed"
                db.commit()
                continue
            try:
                body = _compose_body(tenant, lead, msg.body)
                sender = f"{tenant.from_name} <{tenant.from_email}>"
                unsubscribe_url = f"{s.public_base_url.rstrip('/')}/u/{lead.unsubscribe_token}"
                provider_id, message_id = smtp2go.send(
                    api_key=api_key,
                    sender=sender,
                    to=lead.email,
                    subject=msg.subject,
                    text_body=body,
                    reply_to=tenant.reply_to or tenant.from_email,
                    unsubscribe_url=unsubscribe_url,
                    unsubscribe_mailto=tenant.reply_to or tenant.from_email,
                    sender_domain=tenant.from_email.rsplit("@", 1)[-1],
                )
                msg.status = "sent"
                msg.sent_at = now
                msg.provider_id = provider_id
                msg.message_id_header = message_id
                scheduling.bump_counter(db, tenant)
                lead.last_sent_at = now
                lead.step += 1
                steps = lead.campaign.steps or [{}]
                if lead.step >= len(steps):
                    lead.status = "finished"
                    lead.next_send_at = None
                else:
                    lead.status = "in_sequence"
                    lead.next_send_at = scheduling.schedule_after(tenant, int(steps[lead.step].get("delay_days", 3)), now)
                log_event(db, "sent", f"Schritt {msg.step + 1} an {lead.email}", tenant.id, lead.id)
                sent += 1
                remaining -= 1
            except smtp2go.SendError as exc:
                msg.status = "failed"
                msg.error = str(exc)[:1000]
                lead.last_error = str(exc)[:500]
                log_event(db, "send_failed", str(exc), tenant.id, lead.id)
            db.commit()
            if remaining <= 0:
                break
    return sent


def _compose_body(tenant: Tenant, lead: Lead, body: str) -> str:
    s = get_settings()
    unsubscribe_url = f"{s.public_base_url.rstrip('/')}/u/{lead.unsubscribe_token}"
    parts = [body.strip()]
    if tenant.signature.strip():
        parts.append(tenant.signature.strip())
    footer = tenant.footer.strip()
    parts.append((footer + "\n\n" if footer else "") + f"Keine weiteren Mails von uns gewünscht? Hier abmelden: {unsubscribe_url}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- replies
def poll_replies(db: Session) -> int:
    handled = 0
    tenants = db.query(Tenant).filter(Tenant.active.is_(True), Tenant.imap_host.isnot(None)).all()
    for tenant in tenants:
        password = crypto.decrypt(tenant.imap_password_enc)
        if not (tenant.imap_user and password):
            continue
        try:
            inbound = imap_replies.fetch_unseen(tenant.imap_host, tenant.imap_port, tenant.imap_user, password, tenant.imap_folder)
        except Exception as exc:
            log.warning("IMAP %s: %s", tenant.slug, exc)
            log_event(db, "imap_error", str(exc)[:500], tenant.id)
            db.commit()
            continue
        seen_uids: list[str] = []
        for mail in inbound:
            lead = _match_lead(db, tenant, mail)
            if lead is None:
                continue  # not ours; leave unseen for the human mailbox
            seen_uids.append(mail.uid)
            try:
                cls = llm.classify_reply(mail.subject, mail.body)
            except Exception as exc:
                log.warning("classify failed: %s", exc)
                cls = "other"
            db.add(
                Message(
                    lead_id=lead.id,
                    tenant_id=tenant.id,
                    direction="in",
                    step=lead.step,
                    subject=mail.subject[:300],
                    body=mail.body,
                    status="received",
                    from_email=mail.from_email,
                    classification=cls,
                )
            )
            if cls in ("out_of_office", "auto_reply"):
                log_event(db, "reply_auto", f"{cls} von {mail.from_email}", tenant.id, lead.id)
            else:
                lead.status = "replied"
                lead.next_send_at = None
                for m in lead.messages:
                    if m.direction == "out" and m.status in ("draft", "approved"):
                        m.status = "failed"
                        m.error = "Antwort erhalten, Sequenz gestoppt"
                if cls == "unsubscribe":
                    suppress(db, tenant.id, lead.email or mail.from_email, "unsubscribe")
                elif cls == "bounce":
                    suppress(db, tenant.id, lead.email or mail.from_email, "bounce")
                log_event(db, "reply", f"{cls} von {mail.from_email}: {mail.subject}", tenant.id, lead.id)
                _handoff(tenant, lead, mail, cls)
            handled += 1
            db.commit()
        try:
            imap_replies.mark_seen(tenant.imap_host, tenant.imap_port, tenant.imap_user, password, tenant.imap_folder, seen_uids)
        except Exception as exc:
            log.warning("IMAP mark seen %s: %s", tenant.slug, exc)
    return handled


def _match_lead(db: Session, tenant: Tenant, mail: imap_replies.Inbound) -> Lead | None:
    refs = " ".join(filter(None, [mail.in_reply_to, mail.references]))
    if refs:
        out = (
            db.query(Message)
            .filter(Message.tenant_id == tenant.id, Message.direction == "out", Message.message_id_header.isnot(None))
            .filter(Message.message_id_header.in_([r for r in refs.split() if r]))
            .first()
        )
        if out:
            return out.lead
    if mail.from_email:
        lead = (
            db.query(Lead)
            .filter(Lead.tenant_id == tenant.id, Lead.email == mail.from_email, Lead.last_sent_at.isnot(None))
            .order_by(Lead.last_sent_at.desc())
            .first()
        )
        if lead:
            return lead
        # same domain, personal reply from a different mailbox
        dom = mail.from_email.rsplit("@", 1)[-1]
        if dom and dom not in ("gmail.com", "bluewin.ch", "gmx.ch", "gmx.net", "hotmail.com", "outlook.com", "yahoo.com"):
            return (
                db.query(Lead)
                .filter(Lead.tenant_id == tenant.id, Lead.domain == dom, Lead.last_sent_at.isnot(None))
                .order_by(Lead.last_sent_at.desc())
                .first()
            )
    return None


def _handoff(tenant: Tenant, lead: Lead, mail: imap_replies.Inbound, cls: str) -> None:
    token = crypto.decrypt(tenant.zammad_token_enc)
    if not (tenant.zammad_url and token and tenant.zammad_group):
        return
    if cls in ("bounce",):
        return
    note = (
        f"Outreach-Antwort ({cls}) auf Kampagne '{lead.campaign.name}'.\n"
        f"Firma: {lead.company}\nWebseite: {lead.website or '-'}\nTelefon: {lead.phone or '-'}\n"
        f"Ansprechperson: {lead.contact_name or '-'}"
    )
    try:
        zammad.create_ticket(tenant.zammad_url, token, tenant.zammad_group, mail.from_email, mail.subject or f"Antwort {lead.company}", mail.body, note)
    except Exception as exc:
        log.warning("Zammad handoff failed: %s", exc)


# ---------------------------------------------------------------- webhooks
def handle_webhook(db: Session, tenant: Tenant, payload: dict) -> str:
    event, rcpt, email_id, message_id = smtp2go.parse_webhook(payload)
    msg = None
    if email_id:
        msg = db.query(Message).filter(Message.tenant_id == tenant.id, Message.provider_id == str(email_id)).first()
    if msg is None and message_id:
        msg = db.query(Message).filter(Message.tenant_id == tenant.id, Message.message_id_header == message_id).first()
    lead = msg.lead if msg else None
    target = rcpt or (lead.email if lead else None)
    reason = smtp2go.SUPPRESS_EVENTS.get(event or "")
    if reason and target:
        suppress(db, tenant.id, target, reason)
        log_event(db, f"webhook_{event}", f"{target} ({email_id or message_id or '-'})", tenant.id, lead.id if lead else None)
        return f"suppressed {target} ({reason})"
    log_event(db, f"webhook_{event or 'unknown'}", f"{target or '-'} ({email_id or message_id or '-'})", tenant.id, lead.id if lead else None)
    return "ok"
