"""Health check for the worker container, which has no HTTP server.

The worker touches a heartbeat file after every loop pass; this check fails when
the file is missing or older than WORKER_HEALTH_MAX_AGE seconds (default 900,
long enough for a slow sourcing pass). Exit 0 = healthy, 1 = unhealthy.
"""

from __future__ import annotations

import os
import sys
import time

HEARTBEAT = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/outreach-worker.heartbeat")


def max_age() -> int:
    try:
        return max(60, int(os.environ.get("WORKER_HEALTH_MAX_AGE", "900")))
    except ValueError:
        return 900


def beat(path: str = HEARTBEAT) -> None:
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass  # a missing heartbeat only marks the container unhealthy


def check(path: str = HEARTBEAT, now: float | None = None) -> tuple[bool, str]:
    try:
        age = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        return False, f"kein Heartbeat ({path})"
    if age > max_age():
        return False, f"letzter Durchlauf vor {int(age)}s (Limit {max_age()}s)"
    return True, f"letzter Durchlauf vor {int(age)}s"


if __name__ == "__main__":
    ok, msg = check()
    print(msg)
    sys.exit(0 if ok else 1)
