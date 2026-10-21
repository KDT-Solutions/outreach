from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    _ensure_columns()
    _backfill_unsubscribes()


def _backfill_unsubscribes() -> None:
    """Unsubscribes from before unsubscribed_at existed: take the time from the
    suppression entry and the source from the SMTP2GO webhook event, if any.
    Idempotent: only touches rows without unsubscribed_at."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE leads SET unsubscribed_at = (SELECT s.created_at FROM suppressions s "
            "WHERE s.tenant_id = leads.tenant_id AND s.value = lower(leads.email) AND s.reason = 'unsubscribe') "
            "WHERE status = 'unsubscribed' AND unsubscribed_at IS NULL AND email IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE leads SET unsubscribe_source = CASE WHEN EXISTS (SELECT 1 FROM events e "
            "WHERE e.lead_id = leads.id AND e.kind = 'webhook_unsubscribe') THEN 'webhook' ELSE 'link' END "
            "WHERE status = 'unsubscribed' AND unsubscribe_source IS NULL"
        ))


def _ensure_columns() -> None:
    """Poor man's migration: add columns introduced after the first release."""
    from sqlalchemy import inspect, text

    wanted = {
        "tenants": {"zammad_last_poll": "TIMESTAMP WITH TIME ZONE"},
        "messages": {"zammad_ticket_id": "INTEGER"},
        "leads": {
            "email_candidates": "TEXT",
            "team_excerpt": "TEXT",
            "first_name": "VARCHAR(80)",
            "last_name": "VARCHAR(80)",
            "city": "VARCHAR(120)",
            "language": "VARCHAR(10)",
            "deleted_at": "TIMESTAMP WITH TIME ZONE",
            "unsubscribe_reason": "VARCHAR(40)",
            "unsubscribe_comment": "TEXT",
            "unsubscribed_at": "TIMESTAMP WITH TIME ZONE",
            "unsubscribe_source": "VARCHAR(20)",
        },
        "campaigns": {
            "from_name": "VARCHAR(120)",
            "from_email": "VARCHAR(200)",
            "reply_to": "VARCHAR(200)",
            "signature_override": "TEXT",
            "unsubscribe_text_override": "TEXT",
        },
    }
    obsolete = {"tenants": ["imap_host", "imap_port", "imap_user", "imap_password_enc", "imap_folder"]}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        for table, cols in obsolete.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for col in cols:
                if col in existing:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Session:  # type: ignore[misc]
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
