"""Bearer token, source-IP allowlist, HTTPS check and per-token rate limit for /api/v1 and /mcp.

Pure ASGI middleware so it sits in front of both the FastAPI routes and the MCP
transport. The token comes from OUTREACH_API_TOKEN (or OUTREACH_API_TOKEN_FILE)."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
PROTECTED_PREFIXES = ("/api/v1", "/mcp")
# failed auth attempts per source IP and minute before 429
FAILED_AUTH_LIMIT = 20


@dataclass(frozen=True)
class Principal:
    token_id: str
    # None = every tenant
    tenants: frozenset[str] | None

    def may_access(self, slug: str) -> bool:
        return self.tenants is None or slug in self.tenants


class ConfigError(RuntimeError):
    pass


def _read_token() -> str:
    s = get_settings()
    token = s.outreach_api_token.strip()
    if not token and s.outreach_api_token_file:
        token = Path(s.outreach_api_token_file).read_text().strip()
    return token


def token_id_of(token: str) -> str:
    """Stable, non-reversible identifier for logs and the audit table."""
    return "tok_" + hashlib.sha256(token.encode()).hexdigest()[:12]


@lru_cache
def configured() -> tuple[bytes, Principal]:
    """(sha256 of the token, principal). Raises ConfigError if the token is unusable."""
    token = _read_token()
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"OUTREACH_API_TOKEN fehlt oder ist kürzer als {MIN_TOKEN_LENGTH} Zeichen")
    raw = get_settings().outreach_api_tenants.strip()
    if raw == "*":
        tenants = None
    else:
        tenants = frozenset(t.strip().lower() for t in raw.split(",") if t.strip())
    return hashlib.sha256(token.encode()).digest(), Principal(token_id=token_id_of(token), tenants=tenants)


def check_config() -> None:
    """Fail at startup on a missing/short token or a malformed IP allowlist."""
    configured()
    try:
        _allowed_networks()
    except ValueError as exc:
        raise ConfigError(f"OUTREACH_API_ALLOW_IPS: {exc}") from None


@lru_cache
def _allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = get_settings().outreach_api_allow_ips
    return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]


def ip_allowed(ip: str | None) -> bool:
    nets = _allowed_networks()
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return any(addr in net for net in nets)


def authenticate(header: str) -> Principal | None:
    try:
        digest, principal = configured()
    except ConfigError:
        return None
    if not header.startswith("Bearer "):
        return None
    # compare fixed-length digests: constant time regardless of the token's length
    given = hashlib.sha256(header[7:].strip().encode()).digest()
    return principal if hmac.compare_digest(given, digest) else None


class RateLimiter:
    """Sliding window, in memory. The outreach-mcp service runs a single process."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window: float = 60.0) -> float:
        """Record a hit. Returns 0 if allowed, otherwise seconds until the next slot."""
        now = time.monotonic()
        if len(self._hits) > 10_000:  # forget idle keys (e.g. many failing source IPs)
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
                del self._hits[k]
        q = self._hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, window - (now - q[0]))
        q.append(now)
        return 0.0

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode()


class ApiGuard:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(PROTECTED_PREFIXES):
            return await self.app(scope, receive, send)
        s = get_settings()
        client_ip = (scope.get("client") or (None,))[0]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        if s.outreach_api_require_https and scope.get("scheme") != "https":
            return await _respond(send, 403, "https_required", "Nur über HTTPS erreichbar")
        if not ip_allowed(client_ip):
            return await _respond(send, 403, "forbidden", "Forbidden")
        principal = authenticate(headers.get("authorization", ""))
        if principal is None:
            if limiter.hit(f"fail:{client_ip}", FAILED_AUTH_LIMIT):
                return await _respond(send, 429, "rate_limited", "Zu viele Anfragen", retry_after=60)
            return await _respond(send, 401, "unauthorized", "Unauthorized", www_auth=True)
        wait = limiter.hit(f"tok:{principal.token_id}", s.outreach_api_rate_limit)
        if wait:
            return await _respond(send, 429, "rate_limited", "Zu viele Anfragen", retry_after=int(wait) + 1)

        state = scope.setdefault("state", {})
        state["principal"] = principal
        state["client_ip"] = client_ip
        return await self.app(scope, receive, send)


async def _respond(send, status: int, code: str, message: str, retry_after: int | None = None, www_auth: bool = False) -> None:
    body = _error_body(code, message)
    headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    if retry_after:
        headers.append((b"retry-after", str(retry_after).encode()))
    if www_auth:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
