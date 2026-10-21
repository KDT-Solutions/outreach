"""The outreach pipeline. Each function does one bounded unit of work on a DB session and
is called by the worker loop (and by UI actions)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .models import ApiAudit, Campaign, Event, Lead, Message, Suppression, Tenant, utcnow
from .services import crawler, hunter, llm, mailtext, places, scheduling, signature, smtp2go, templating, verify, zammad

log = logging.getLogger(__name__)


def log_event(db: Session, kind: str, detail: str = "", tenant_id: int | None = None, lead_id: int | None = None) -> None:
    db.add(Event(kind=kind, detail=detail[:4000], tenant_id=tenant_id, lead_id=lead_id))


def audit(db: Session, tool: str, actor: str, transport: str, tenant_id: int | None, ids: dict | None = None,
          detail: str = "", client_ip: str | None = None) -> None:
    """Audit entry for a write action outside the automation API (web UI, old /api)."""
    db.add(ApiAudit(token_id=actor[:40], tenant_id=tenant_id, tool=tool[:60], transport=transport, client_ip=client_ip,
                    outcome="applied", object_ids=ids or {}, detail=detail[:2000]))


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
        if reason == "unsubscribe" and lead.unsubscribed_at is None:
            lead.unsubscribed_at = utcnow()
        if lead.status in ("needs_review", "scheduled", "in_sequence", "enriched"):
            lead.status = "unsubscribed" if reason == "unsubscribe" else "bounced" if reason in ("bounce", "spam") else "stopped"
            lead.next_send_at = None
            for m in lead.messages:
                if m.direction == "out" and m.status in ("draft", "approved"):
                    m.status = "failed"
                    m.error = f"suppressed: {reason}"


def discard_pending(lead: Lead, reason: str) -> int:
    """Drop unsent drafts/approved mails of a lead so the worker writes them again.
    Returns the number of discarded messages."""
    n = 0
    for m in lead.messages:
        if m.direction == "out" and m.status in ("draft", "approved"):
            m.status = "failed"
            m.error = reason
            n += 1
    if n and lead.status in ("needs_review", "scheduled"):
        lead.status = "enriched" if lead.step == 0 else "in_sequence"
        if lead.step == 0:
            lead.next_send_at = None
    return n


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
@dataclass
class EmailFinding:
    email: str | None
    source: str | None  # crawler | hunter
    crawl: crawler.CrawlResult
    contact_name: str | None = None


def find_email(website: str, place_hint: str | None = None, **crawl_opts) -> EmailFinding:
    """Crawl the website for an address, Hunter.io as fallback. No DB access, so it can run
    in worker threads (API search/enrich). crawl_opts go to crawler.crawl."""
    res = crawler.crawl(website, place_hint=place_hint, **crawl_opts)
    if res.best_email:
        return EmailFinding(res.best_email, "crawler", res)
    if res.domain:
        best = hunter.pick_best(hunter.domain_search(res.domain))
        if best and best.get("value"):
            name = f"{best['first_name']} {best['last_name']}" if best.get("first_name") and best.get("last_name") else None
            return EmailFinding(best["value"].lower(), "hunter", res, name)
    return EmailFinding(None, None, res)


def enrich_lead(db: Session, lead: Lead) -> None:
    """Find an e-mail address and website excerpts for one lead."""
    if is_suppressed(db, lead.tenant_id, lead.email):
        lead.status = "stopped"
        lead.notes = "Adresse auf Sperrliste"
        return
    if lead.email_source == "import" and lead.email:
        # imported with a known address: nothing to crawl, only the MX check
        lead.email_mx_ok = verify.check(lead.email)
        if not lead.email_mx_ok:
            lead.status = "no_email"
            lead.last_error = f"{lead.email}: kein MX-Eintrag"
            return
        lead.status = "enriched"
        return
    if not lead.website:
        lead.status = "no_email"
        lead.notes = "Keine Webseite bei Google hinterlegt"
        return

    found = find_email(lead.website, place_hint=lead.address)
    res = found.crawl
    lead.domain = res.domain or lead.domain
    lead.site_excerpt = res.site_excerpt or None
    lead.impressum_excerpt = res.impressum_excerpt or None
    lead.team_excerpt = res.team_excerpt or None
    email, source = found.email, found.source
    lead.email_candidates = ",".join(res.emails[:10]) or None
    if found.contact_name:
        lead.contact_name = found.contact_name

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
    if templating.is_template_step(steps[lead.step]):
        return _render_for_lead(db, lead, steps[lead.step])
    previous = [(m.subject, m.body) for m in lead.messages if m.direction == "out" and m.status == "sent"]
    draft = llm.write_mail(tenant, campaign, lead, lead.step, previous)
    if draft.contact_name and not lead.contact_name:
        lead.contact_name = draft.contact_name[:120]
    # the Impressum named a person: switch to their mailbox if the crawler saw one
    if lead.step == 0 and lead.contact_name and lead.email_candidates and lead.email_source == "crawler":
        better = crawler.pick_for_contact(lead.email_candidates.split(","), lead.contact_name)
        if better and better != lead.email and not is_suppressed(db, tenant.id, better) and verify.check(better):
            log_event(db, "email_switch", f"{lead.email} -> {better} (passt zu {lead.contact_name})", tenant.id, lead.id)
            lead.email = better
    if draft.salutation and not lead.salutation:
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


def _render_for_lead(db: Session, lead: Lead, step: dict) -> Message:
    """Fixed-text step: fill the placeholders. A placeholder without a value always
    sends the mail to the review queue, even with auto-approve."""
    campaign = lead.campaign
    tenant = campaign.tenant
    subject, body, missing = templating.render_step(step, lead)
    msg = Message(lead_id=lead.id, tenant_id=tenant.id, direction="out", step=lead.step, subject=subject[:300], body=body)
    if campaign.auto_approve and not missing:
        msg.status = "approved"
        msg.approved_at = datetime.now(timezone.utc)
        lead.status = "scheduled" if lead.step == 0 else "in_sequence"
        if lead.next_send_at is None:
            lead.next_send_at = scheduling.schedule_after(tenant, 0)
    else:
        msg.status = "draft"
        lead.status = "needs_review"
        if missing:
            log_event(db, "template_missing", "Leere Platzhalter: " + ", ".join(missing), tenant.id, lead.id)
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
            lead.last_error = None
            n += 1
        except Exception as exc:
            log.exception("generate lead %s", lead.id)
            err = f"{type(exc).__name__}: {exc}"
            lead.last_error = err[:500]
            log_event(db, "generate_failed", err, lead.tenant_id, lead.id)
            db.flush()
            failures = generate_failures(db, lead.id)
            if failures >= MAX_GENERATE_FAILURES:
                # stop retrying every worker cycle; the lead waits for a manual fix
                lead.status = "draft_failed"
                log_event(db, "generate_gave_up", f"Nach {failures} Fehlversuchen angehalten", lead.tenant_id, lead.id)
        db.commit()
    return n


MAX_GENERATE_FAILURES = 3


def generate_failures(db: Session, lead_id: int) -> int:
    """Failed draft attempts since the lead was last changed (a change resets the count)."""
    reset_at = db.scalar(
        select(func.max(Event.created_at)).where(Event.lead_id == lead_id, Event.kind == "lead_reset")
    )
    q = select(func.count(Event.id)).where(Event.lead_id == lead_id, Event.kind == "generate_failed")
    if reset_at is not None:
        q = q.where(Event.created_at > reset_at)
    return db.scalar(q) or 0


def reset_generate_failures(db: Session, lead: Lead, reason: str) -> None:
    """Start counting failed attempts from zero and let a parked lead be drafted again."""
    log_event(db, "lead_reset", reason, lead.tenant_id, lead.id)
    if lead.status == "draft_failed":
        lead.status = "enriched"


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
                body = compose_body(lead.campaign, lead, msg.body)
                from_name, from_email, reply_to = lead.campaign.sender
                sender = f"{from_name} <{from_email}>"
                unsubscribe_url = f"{s.public_base_url.rstrip('/')}/u/{lead.unsubscribe_token}"
                provider_id, message_id = smtp2go.send(
                    api_key=api_key,
                    sender=sender,
                    to=lead.email,
                    subject=msg.subject,
                    text_body=body,
                    reply_to=reply_to,
                    unsubscribe_url=unsubscribe_url,
                    unsubscribe_mailto=reply_to,
                    sender_domain=from_email.rsplit("@", 1)[-1],
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


def compose_body(campaign: Campaign, lead: Lead, body: str) -> str:
    """Mail body plus signature and unsubscribe line (see services/signature.py)."""
    return signature.compose(campaign, lead, body)


# ---------------------------------------------------------------- replies (via Zammad)
GENERIC_MAIL_DOMAINS = ("gmail.com", "bluewin.ch", "gmx.ch", "gmx.net", "gmx.de", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "sunrise.ch", "hispeed.ch")


def poll_replies(db: Session) -> int:
    """Replies arrive in the tenant's Zammad (the reply-to mailbox is connected there).
    Pull tickets created since the last poll, match them to leads, stop the sequence,
    classify, and leave the lead context as an internal note on the ticket."""
    handled = 0
    tenants = db.query(Tenant).filter(Tenant.active.is_(True), Tenant.zammad_url.isnot(None)).all()
    for tenant in tenants:
        token = crypto.decrypt(tenant.zammad_token_enc)
        if not token:
            continue
        now = datetime.now(timezone.utc)
        since = tenant.zammad_last_poll or (now - timedelta(days=2))
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            tickets = zammad.recent_tickets(tenant.zammad_url, token, since_iso, tenant.zammad_group or None)
        except Exception as exc:
            log.warning("Zammad poll %s: %s", tenant.slug, exc)
            log_event(db, "zammad_error", str(exc)[:500], tenant.id)
            db.commit()
            continue
        for t in tickets:
            tid = t.get("id")
            if not tid or db.query(Message.id).filter(Message.zammad_ticket_id == tid).first():
                continue
            cust = t.get("customer")
            if isinstance(cust, dict):
                from_email = (cust.get("email") or "").lower()
            else:
                from_email = (str(cust or "") if "@" in str(cust or "") else t.get("customer_email") or "").lower()
            lead = _match_lead(db, tenant, from_email)
            if lead is None:
                continue
            art = zammad.first_article(tenant.zammad_url, token, tid) or {}
            body = mailtext.body_from_article(art)
            subject = t.get("title") or ""
            try:
                # classify only the reply itself, not our own quoted mail below it
                cls = llm.classify_reply(subject, mailtext.split_quote(body)[0])
            except Exception as exc:
                log.warning("classify failed: %s", exc)
                cls = "other"
            db.add(
                Message(
                    lead_id=lead.id, tenant_id=tenant.id, direction="in", step=lead.step,
                    subject=subject[:300], body=body[:8000], status="received",
                    from_email=from_email or None, classification=cls, zammad_ticket_id=tid,
                )
            )
            if cls in ("out_of_office", "auto_reply"):
                log_event(db, "reply_auto", f"{cls} von {from_email} (Ticket {t.get('number')})", tenant.id, lead.id)
            else:
                lead.status = "replied"
                lead.next_send_at = None
                for m in lead.messages:
                    if m.direction == "out" and m.status in ("draft", "approved"):
                        m.status = "failed"
                        m.error = "Antwort erhalten, Sequenz gestoppt"
                if cls == "unsubscribe":
                    suppress(db, tenant.id, lead.email or from_email, "unsubscribe")
                elif cls == "bounce":
                    suppress(db, tenant.id, lead.email or from_email, "bounce")
                log_event(db, "reply", f"{cls} von {from_email}: {subject} (Ticket {t.get('number')})", tenant.id, lead.id)
                note = (
                    f"Outreach-Antwort, Einstufung: {cls}\n"
                    f"Kampagne: {lead.campaign.name}\nFirma: {lead.company}\nWebseite: {lead.website or '-'}\n"
                    f"Telefon: {lead.phone or '-'}\nAnsprechperson: {lead.contact_name or '-'}\n"
                    f"Gesendete Schritte: {lead.step}\nLead: {get_settings().public_base_url.rstrip('/')}/leads/{lead.id}"
                )
                try:
                    zammad.add_note(tenant.zammad_url, token, tid, note)
                except Exception as exc:
                    log.warning("Zammad note failed: %s", exc)
            handled += 1
            db.commit()
        tenant.zammad_last_poll = now
        db.commit()
    return handled


def _match_lead(db: Session, tenant: Tenant, from_email: str) -> Lead | None:
    if not from_email or "@" not in from_email:
        return None
    lead = (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant.id, Lead.email == from_email, Lead.last_sent_at.isnot(None))
        .order_by(Lead.last_sent_at.desc())
        .first()
    )
    if lead:
        return lead
    dom = from_email.rsplit("@", 1)[-1]
    if dom in GENERIC_MAIL_DOMAINS:
        return None
    return (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant.id, Lead.domain == dom, Lead.last_sent_at.isnot(None))
        .order_by(Lead.last_sent_at.desc())
        .first()
    )


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
        if reason == "unsubscribe" and lead is not None and lead.unsubscribe_source is None:
            lead.unsubscribe_source = "webhook"
        log_event(db, f"webhook_{event}", f"{target} ({email_id or message_id or '-'})", tenant.id, lead.id if lead else None)
        return f"suppressed {target} ({reason})"
    log_event(db, f"webhook_{event or 'unknown'}", f"{target or '-'} ({email_id or message_id or '-'})", tenant.id, lead.id if lead else None)
    return "ok"


# ---------------------------------------------------------------- one-off repair
REPLY_REPAIR_MARK = "replies_v1"


def repair_replies_once(db: Session) -> int:
    """Replies stored before the fix lost their line breaks, and every rejection was
    stored as "interested" (substring match). Re-read each body from Zammad, classify
    again and leave an internal note on the ticket when the class changes.
    Runs once per database, marked by a "migration" event."""
    if db.scalar(select(Event.id).where(Event.kind == "migration", Event.detail == REPLY_REPAIR_MARK)):
        return 0
    fixed = 0
    msgs = db.query(Message).filter(Message.direction == "in", Message.zammad_ticket_id.isnot(None)).all()
    for m in msgs:
        tenant = db.get(Tenant, m.tenant_id)
        token = crypto.decrypt(tenant.zammad_token_enc) if tenant and tenant.zammad_url else None
        if not token:
            continue
        try:
            art = zammad.first_article(tenant.zammad_url, token, m.zammad_ticket_id)
            if art:
                m.body = mailtext.body_from_article(art)[:8000]
            old = m.classification
            new = llm.classify_reply(m.subject or "", mailtext.split_quote(m.body or "")[0])
        except Exception as exc:
            log.warning("repair reply %s: %s", m.id, exc)
            db.rollback()
            continue
        if new != old:
            m.classification = new
            fixed += 1
            log_event(db, "reply_reclassified", f"{old} -> {new}", m.tenant_id, m.lead_id)
            if new in ("unsubscribe", "bounce") and m.lead and (m.lead.email or m.from_email):
                suppress(db, m.tenant_id, m.lead.email or m.from_email, new)
            try:
                zammad.add_note(
                    tenant.zammad_url, token, m.zammad_ticket_id,
                    f"Outreach: Einstufung korrigiert von {old} auf {new} "
                    f"(Fehler in der automatischen Einstufung, inzwischen behoben).",
                )
            except Exception as exc:
                log.warning("Zammad note failed: %s", exc)
        db.commit()
    log_event(db, "migration", REPLY_REPAIR_MARK)
    db.commit()
    return fixed
