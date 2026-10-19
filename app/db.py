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


def _ensure_columns() -> None:
    """Poor man's migration: add columns introduced after the first release."""
    from sqlalchemy import inspect, text

    wanted = {
        "tenants": {"zammad_last_poll": "TIMESTAMP WITH TIME ZONE"},
        "messages": {"zammad_ticket_id": "INTEGER"},
    }
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


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
