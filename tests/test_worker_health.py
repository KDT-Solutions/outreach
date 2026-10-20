import os
import time

from app import healthcheck


def test_missing_heartbeat_is_unhealthy(tmp_path):
    ok, msg = healthcheck.check(str(tmp_path / "nope"))
    assert not ok and "kein Heartbeat" in msg


def test_fresh_and_stale_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_HEALTH_MAX_AGE", "300")
    hb = tmp_path / "hb"
    healthcheck.beat(str(hb))
    assert healthcheck.check(str(hb))[0]
    old = time.time() - 301
    os.utime(hb, (old, old))
    assert not healthcheck.check(str(hb))[0]
