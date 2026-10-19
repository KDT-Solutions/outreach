"""Background loop: sourcing -> enrichment -> text generation -> sending -> reply polling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import healthcheck, pipeline
from .config import get_settings
from .db import SessionLocal, init_db
from .models import Campaign

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

_last_reply_poll = 0.0


def tick() -> None:
    global _last_reply_poll
    s = get_settings()
    with SessionLocal() as db:
        # 1. sourcing for active campaigns that haven't run yet
        for campaign in db.query(Campaign).filter(Campaign.status == "active", Campaign.sourcing_done.is_(False)).all():
            try:
                log.info("sourcing campaign %s: %s", campaign.id, pipeline.run_sourcing(db, campaign))
            except Exception:
                log.exception("sourcing failed for campaign %s", campaign.id)
                db.rollback()
            db.commit()

        # 2. enrichment
        n = pipeline.enrich_batch(db, s.enrich_batch)
        if n:
            log.info("enriched %d leads", n)

        # 3. generation
        n = pipeline.generate_batch(db, s.generate_batch)
        if n:
            log.info("generated %d drafts", n)

        # 4. sending
        n = pipeline.send_due(db, s.send_batch)
        if n:
            log.info("sent %d mails", n)

        # 5. replies from Zammad, every 2 minutes
        if time.time() - _last_reply_poll > 120:
            _last_reply_poll = time.time()
            try:
                n = pipeline.poll_replies(db)
                if n:
                    log.info("handled %d replies", n)
            except Exception:
                log.exception("reply polling failed")
                db.rollback()


def main() -> None:
    init_db()
    interval = get_settings().worker_interval_seconds
    log.info("worker started, interval %ss", interval)
    healthcheck.beat()
    while True:
        started = time.time()
        try:
            tick()
        except Exception:
            log.exception("tick failed")
        healthcheck.beat()
        time.sleep(max(5, interval - (time.time() - started)))


if __name__ == "__main__":
    main()
