"""Single-admin login with signed session cookie, CSRF token, and Bearer token for the API."""

from __future__ import annotations

import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

COOKIE = "outreach_session"
SESSION_MAX_AGE = 12 * 3600
_failed: dict[str, list[float]] = {}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="session")


def create_session() -> str:
    return _serializer().dumps({"u": get_settings().admin_user, "csrf": secrets.token_urlsafe(16)})


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def check_login(username: str, password: str, client_ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed.get(client_ip, []) if now - t < 900]
    if len(attempts) >= 8:
        return False
    s = get_settings()
    ok = hmac.compare_digest(username.encode(), s.admin_user.encode()) and hmac.compare_digest(password.encode(), s.admin_password.encode())
    if not ok:
        attempts.append(now)
        _failed[client_ip] = attempts
    else:
        _failed.pop(client_ip, None)
    return ok


def require_login(request: Request) -> dict:
    sess = read_session(request)
    if not sess:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return sess


async def require_csrf(request: Request, sess: dict = Depends(require_login)) -> dict:
    """For HTML form POSTs: token must match the session's."""
    form = await request.form()
    token = form.get("csrf", "")
    if not hmac.compare_digest(str(token), sess.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF-Token ungültig")
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    base = get_settings().public_base_url.rstrip("/")
    if origin and not (origin.startswith(base) or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")):
        raise HTTPException(status_code=403, detail="Origin nicht erlaubt")
    return sess


def require_api_token(request: Request) -> None:
    s = get_settings()
    auth = request.headers.get("authorization", "")
    if not s.api_token or not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], s.api_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
