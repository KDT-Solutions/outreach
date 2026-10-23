"""Single-admin login (argon2 password hash or plain password, optional TOTP), signed session
cookie, CSRF token, and Bearer token for the old JSON API.

Brute-force protection works on two levels, both independent of the client IP, because the IP
is only as trustworthy as the proxy chain in front of uvicorn (X-Forwarded-For):

* Account lock-out per user name: after LOCK_AFTER failed password or TOTP attempts the account
  is locked for a delay that doubles with every further failure (LOCK_BASE .. LOCK_MAX seconds).
  While locked, no credential is verified at all. The lock is cleared by a successful login or
  when the delay has passed.
* Per-IP limit on top of that (IP_MAX_ATTEMPTS per IP_WINDOW seconds), so a single misbehaving
  client is cut off earlier than the account lock would.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

log = logging.getLogger(__name__)

COOKIE = "outreach_session"
PRE_COOKIE = "outreach_pre"  # password verified, TOTP pending (second step)
SESSION_MAX_AGE = 12 * 3600
PRE_MAX_AGE = 300

# account lock-out (independent of the client address)
LOCK_AFTER = 5          # failed attempts before the first lock
LOCK_BASE = 60          # seconds for the first lock, doubles per further failure
LOCK_MAX = 900          # cap for the lock duration
FAIL_MEMORY = 3600      # a failure older than this no longer counts
ACCOUNTS_MAX = 500      # user names tracked at most (unknown names are tried by scanners)

# per-IP limit (secondary; the address may be spoofed if the proxy chain is misconfigured)
IP_MAX_ATTEMPTS = 8
IP_WINDOW = 900

_ph = PasswordHasher()
_lock = threading.Lock()
_failed: dict[str, list[float]] = {}
_accounts: dict[str, dict] = {}


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


# --- admin credentials -------------------------------------------------------------------

def validate_admin_config() -> list[str]:
    """Warnings about the admin credentials; raises when no password is configured at all.
    Called by the UI app at start-up."""
    s = get_settings()
    if not s.admin_password_hash and not s.admin_password:
        raise RuntimeError("ADMIN_PASSWORD_HASH_B64 (oder ADMIN_PASSWORD) fehlt")
    warnings = []
    if s.admin_password_hash:
        try:
            _ph.check_needs_rehash(s.admin_password_hash)
        except Exception:
            raise RuntimeError("ADMIN_PASSWORD_HASH ist kein argon2-Hash (python -m app.tools hash-password)") from None
        if s.admin_password:
            warnings.append("ADMIN_PASSWORD wird ignoriert, weil ADMIN_PASSWORD_HASH gesetzt ist")
    else:
        warnings.append("ADMIN_PASSWORD liegt im Klartext in der Umgebung; besser ADMIN_PASSWORD_HASH_B64 setzen (python -m app.tools hash-password)")
    if not s.admin_totp_secret:
        warnings.append("Kein zweiter Faktor (ADMIN_TOTP_SECRET) für den Admin-Login konfiguriert")
    for w in warnings:
        log.warning(w)
    return warnings


def _password_ok(password: str) -> bool:
    s = get_settings()
    if s.admin_password_hash:
        try:
            return _ph.verify(s.admin_password_hash, password)
        except (VerifyMismatchError, ValueError):
            return False
    return bool(s.admin_password) and hmac.compare_digest(password.encode(), s.admin_password.encode())


# --- account lock-out -------------------------------------------------------------------

def _account(username: str) -> dict:
    """State for a user name; the table is capped so scanners cannot grow it without bound."""
    key = (username or "").strip().lower()[:200]
    acc = _accounts.get(key)
    if acc is None:
        if len(_accounts) >= ACCOUNTS_MAX:
            oldest = min(_accounts, key=lambda k: _accounts[k]["last_failure"])
            _accounts.pop(oldest, None)
        acc = _accounts[key] = {"failures": 0, "locked_until": 0.0, "last_failure": 0.0}
    return acc


def locked_for(username: str) -> int:
    """Seconds the account stays locked, 0 when open."""
    with _lock:
        acc = _accounts.get((username or "").strip().lower()[:200])
        return max(0, int(acc["locked_until"] - time.time())) if acc else 0


def _account_blocked(username: str) -> bool:
    now = time.time()
    with _lock:
        acc = _account(username)
        if now - acc["last_failure"] > FAIL_MEMORY:
            acc["failures"] = 0
        return acc["locked_until"] > now


def _account_record(username: str, ok: bool, client_ip: str) -> None:
    now = time.time()
    with _lock:
        if ok:
            _accounts.pop((username or "").strip().lower()[:200], None)
            return
        acc = _account(username)
        acc["failures"] += 1
        acc["last_failure"] = now
        if acc["failures"] >= LOCK_AFTER:
            delay = min(LOCK_MAX, LOCK_BASE * 2 ** (acc["failures"] - LOCK_AFTER))
            acc["locked_until"] = now + delay
            log.warning("login for %r locked for %ss after %s failures (ip %s)", username, delay, acc["failures"], client_ip)


def reset_lockout() -> None:
    """For tests."""
    with _lock:
        _accounts.clear()
        _failed.clear()


# --- per-IP limit ----------------------------------------------------------------------

def _too_many(client_ip: str) -> bool:
    now = time.time()
    with _lock:
        attempts = [t for t in _failed.get(client_ip, []) if now - t < IP_WINDOW]
        _failed[client_ip] = attempts
        return len(attempts) >= IP_MAX_ATTEMPTS


def _record(username: str, client_ip: str, ok: bool) -> None:
    with _lock:
        if not ok:
            _failed.setdefault(client_ip, []).append(time.time())
        else:
            _failed.pop(client_ip, None)
    _account_record(username, ok, client_ip)


def blocked(username: str, client_ip: str) -> bool:
    """True while no credential may be checked (account locked or this address over its limit)."""
    return _account_blocked(username) or _too_many(client_ip)


def check_password(username: str, password: str, client_ip: str) -> bool:
    """Step 1: user name and password."""
    if blocked(username, client_ip):
        return False
    s = get_settings()
    ok = hmac.compare_digest(username.encode(), s.admin_user.encode())
    ok = _password_ok(password) and ok
    _record(username, client_ip, ok)
    return ok


def check_totp(username: str, code: str, client_ip: str) -> bool:
    """Step 2: one-time code (only when a TOTP secret is configured)."""
    if blocked(username, client_ip):
        return False
    s = get_settings()
    ok = bool(s.admin_totp_secret) and pyotp.TOTP(s.admin_totp_secret).verify((code or "").replace(" ", ""), valid_window=1)
    _record(username, client_ip, ok)
    return ok


def totp_required() -> bool:
    return bool(get_settings().admin_totp_secret)


def check_login(username: str, password: str, client_ip: str, totp: str = "") -> bool:
    """Single-step check (tests): password and, if configured, TOTP together."""
    if not check_password(username, password, client_ip):
        return False
    return check_totp(username, totp, client_ip) if totp_required() else True


def create_pre_session() -> str:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="pre").dumps({"u": get_settings().admin_user, "n": secrets.token_urlsafe(8)})


def read_pre_session(request: Request) -> dict | None:
    raw = request.cookies.get(PRE_COOKIE)
    if not raw:
        return None
    try:
        return URLSafeTimedSerializer(get_settings().secret_key, salt="pre").loads(raw, max_age=PRE_MAX_AGE)
    except BadSignature:
        return None


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


def safe_next(path: str | None, default: str = "/") -> str:
    """Redirect target from a form field: relative paths only (no open redirect)."""
    p = (path or "").strip()
    if not p.startswith("/") or p.startswith(("//", "/\\")) or "\n" in p or "\r" in p:
        return default
    return p


def require_api_token(request: Request) -> None:
    s = get_settings()
    auth = request.headers.get("authorization", "")
    if not s.api_token or not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], s.api_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
