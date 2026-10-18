"""Send-window logic: business hours, weekdays, per-tenant daily cap, jitter."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..models import DailyCounter, Tenant


def tz_of(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "Europe/Zurich")
    except Exception:
        return ZoneInfo("Europe/Zurich")


def local_now(tenant: Tenant) -> datetime:
    return datetime.now(tz_of(tenant))


def in_window(tenant: Tenant, now: datetime | None = None) -> bool:
    now = now or local_now(tenant)
    if tenant.send_weekdays_only and now.weekday() >= 5:
        return False
    return tenant.send_hour_start <= now.hour < tenant.send_hour_end


def next_window_start(tenant: Tenant, after: datetime | None = None) -> datetime:
    """Earliest local datetime >= after that lies inside the send window, with jitter."""
    now = after or local_now(tenant)
    now = now.astimezone(tz_of(tenant))
    candidate = now
    for _ in range(14):
        if in_window(tenant, candidate):
            return candidate + timedelta(minutes=random.randint(0, 45))
        # jump to next day's window start
        start = candidate.replace(hour=tenant.send_hour_start, minute=0, second=0, microsecond=0)
        if candidate < start and (not tenant.send_weekdays_only or candidate.weekday() < 5):
            candidate = start
        else:
            candidate = (start + timedelta(days=1))
    return candidate


def schedule_after(tenant: Tenant, delay_days: int, base: datetime | None = None) -> datetime:
    base = (base or datetime.now(timezone.utc)).astimezone(tz_of(tenant))
    target = base + timedelta(days=delay_days)
    if delay_days > 0:
        target = target.replace(hour=tenant.send_hour_start, minute=0, second=0, microsecond=0)
    return next_window_start(tenant, target).astimezone(timezone.utc)


def today_key(tenant: Tenant) -> str:
    return local_now(tenant).strftime("%Y-%m-%d")


def sent_today(db: Session, tenant: Tenant) -> int:
    row = db.query(DailyCounter).filter_by(tenant_id=tenant.id, day=today_key(tenant)).one_or_none()
    return row.count if row else 0


def bump_counter(db: Session, tenant: Tenant) -> None:
    key = today_key(tenant)
    row = db.query(DailyCounter).filter_by(tenant_id=tenant.id, day=key).with_for_update().one_or_none()
    if row is None:
        row = DailyCounter(tenant_id=tenant.id, day=key, count=0)
        db.add(row)
    row.count += 1
