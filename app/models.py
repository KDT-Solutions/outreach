from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(24)


class Tenant(Base):
    """A sending identity: one company with its own SMTP2GO key, mailbox and limits."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    from_name: Mapped[str] = mapped_column(String(120))
    from_email: Mapped[str] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    # What the company sells, in the operator's words. Fed to the LLM for every campaign.
    company_blurb: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Legal footer (company name + postal address) appended to every mail.
    footer: Mapped[str] = mapped_column(Text, default="")

    smtp2go_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_token: Mapped[str] = mapped_column(String(64), default=new_token)

    # Zammad hand-off (optional)
    zammad_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    zammad_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    zammad_group: Mapped[str | None] = mapped_column(String(100), nullable=True)
    zammad_last_poll: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    daily_limit: Mapped[int] = mapped_column(Integer, default=40)
    send_hour_start: Mapped[int] = mapped_column(Integer, default=9)
    send_hour_end: Mapped[int] = mapped_column(Integer, default=17)
    send_weekdays_only: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(60), default="Europe/Zurich")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    campaigns: Mapped[list[Campaign]] = relationship(back_populates="tenant")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # draft | active | paused | done
    status: Mapped[str] = mapped_column(String(20), default="draft")

    # One Google Places text query per line, e.g. "Autogarage Basel"
    search_queries: Mapped[str] = mapped_column(Text, default="")
    # Who we want to reach and why they care, for the LLM
    target_description: Mapped[str] = mapped_column(Text, default="")
    offer: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(10), default="de")
    tone: Mapped[str] = mapped_column(Text, default="")
    # [{"delay_days": 0, "instruction": "..."}, {"delay_days": 4, "instruction": "..."}]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # Drafts go out without manual approval
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    max_leads: Mapped[int] = mapped_column(Integer, default=200)
    sourcing_done: Mapped[bool] = mapped_column(Boolean, default=False)
    sourcing_log: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="campaigns")
    leads: Mapped[list[Lead]] = relationship(back_populates="campaign")

    @property
    def query_list(self) -> list[str]:
        return [q.strip() for q in self.search_queries.splitlines() if q.strip()]


LEAD_STATUSES = (
    "new",          # sourced, not enriched yet
    "enriched",     # email found, ready for text generation
    "no_email",     # crawler + Hunter found nothing
    "needs_review", # draft waiting for approval
    "scheduled",    # approved, waiting for send window
    "in_sequence",  # at least one mail sent, more steps pending
    "finished",     # all steps sent
    "replied",      # human reply received, sequence stopped
    "bounced",
    "unsubscribed",
    "stopped",      # manually stopped
    "skipped",      # operator skipped
)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "place_id", name="uq_lead_tenant_place"),
        Index("ix_lead_tenant_email", "tenant_id", "email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)

    place_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    place_types: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_query: Mapped[str | None] = mapped_column(String(200), nullable=True)

    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_source: Mapped[str | None] = mapped_column(String(30), nullable=True)  # crawler|hunter|manual
    email_mx_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ranked addresses the crawler found, comma-separated (first 10)
    email_candidates: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    salutation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Text the LLM sees: homepage + impressum/kontakt excerpt
    site_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    impressum_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    step: Mapped[int] = mapped_column(Integer, default=0)  # next step to send (0-based)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), default=new_token, unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")
    messages: Mapped[list[Message]] = relationship(back_populates="lead", order_by="Message.id")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3), default="out")  # out | in
    step: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # out: draft | approved | sent | failed ; in: received
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    message_id_header: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # inbound only
    from_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(30), nullable=True)
    zammad_ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class Suppression(Base):
    """Addresses or domains that must never be mailed again (per tenant)."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("tenant_id", "value", name="uq_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    value: Mapped[str] = mapped_column(String(200))  # email or @domain
    reason: Mapped[str] = mapped_column(String(60))  # unsubscribe|bounce|spam|manual|reply
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class DailyCounter(Base):
    """Sent count per tenant and local day, for the daily limit."""

    __tablename__ = "daily_counters"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_daily_counter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in tenant timezone
    count: Mapped[int] = mapped_column(Integer, default=0)
