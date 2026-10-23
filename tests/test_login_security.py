"""Admin login: account lock-out per user name (independent of the IP), argon2 hash, TOTP,
logout with CSRF, redirect targets, lead website links, webhook suppression scope."""

import base64
import re
from datetime import datetime, timezone
from unittest import mock

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.db import SessionLocal
from app.main import app as ui_app
from app.models import Lead, Message, Suppression, Tenant
from app import pipeline

LOGIN = {"username": "admin", "password": "test"}


@pytest.fixture(autouse=True)
def _fresh_lockout():
    auth.reset_lockout()
    yield
    auth.reset_lockout()


def _ui():
    return TestClient(ui_app, base_url="https://testserver")


def _login(ui, **override):
    return ui.post("/login", data={**LOGIN, **override}, follow_redirects=False)


# ---------------------------------------------------------------- lock-out
def test_account_locks_after_failures_independent_of_ip():
    # five failures from five different addresses: the account is locked, not the addresses
    for i in range(auth.LOCK_AFTER):
        assert auth.check_password("admin", "falsch", f"10.0.0.{i}") is False
    assert auth.locked_for("admin") > 0
    # the right password is refused while locked, from yet another address
    assert auth.check_password("admin", "test", "10.0.1.1") is False
    assert auth.check_password("Admin ", "test", "10.0.1.2") is False  # same account, other spelling
    # another user name is not affected (it cannot log in anyway, but the lock is per name)
    assert auth.locked_for("other") == 0


def test_lock_backs_off_and_clears():
    with mock.patch.object(auth.time, "time", return_value=1_000.0):
        for _ in range(auth.LOCK_AFTER):
            auth.check_password("admin", "falsch", "1.2.3.4")
        assert auth.locked_for("admin") == auth.LOCK_BASE
    # after the lock has passed one more failure doubles the delay
    with mock.patch.object(auth.time, "time", return_value=1_000.0 + auth.LOCK_BASE + 1):
        assert auth.check_password("admin", "falsch", "1.2.3.5") is False
        assert auth.locked_for("admin") == auth.LOCK_BASE * 2
    # a failure older than FAIL_MEMORY no longer counts, a success clears everything
    with mock.patch.object(auth.time, "time", return_value=1_000.0 + auth.FAIL_MEMORY + auth.LOCK_MAX + 10):
        assert auth.check_password("admin", "test", "1.2.3.6") is True
        assert auth.locked_for("admin") == 0


def test_ui_login_answers_429_while_locked():
    ui = _ui()
    for _ in range(auth.LOCK_AFTER - 1):
        assert _login(ui, password="falsch").status_code == 401
    r = _login(ui, password="falsch")
    assert r.status_code == 429 and "gesperrt" in r.text and r.headers.get("retry-after")
    r = _login(ui)
    assert r.status_code == 429 and "set-cookie" not in r.headers


def test_account_table_is_capped():
    for i in range(auth.ACCOUNTS_MAX + 50):
        auth.check_password(f"scan{i}", "x", "9.9.9.9")
    assert len(auth._accounts) <= auth.ACCOUNTS_MAX


# ---------------------------------------------------------------- password hash and TOTP
def test_argon2_hash_replaces_plain_password():
    h = PasswordHasher().hash("geheim-hash")
    with mock.patch.object(get_settings(), "admin_password_hash", h):
        assert auth.check_password("admin", "geheim-hash", "1.1.1.1") is True
        assert auth.check_password("admin", "test", "1.1.1.1") is False  # the plain ADMIN_PASSWORD is ignored
        assert auth.check_password("root", "geheim-hash", "1.1.1.1") is False
        assert any("ADMIN_PASSWORD wird ignoriert" in w for w in auth.validate_admin_config())
    with mock.patch.object(get_settings(), "admin_password_hash", "kein-hash"), pytest.raises(RuntimeError):
        auth.validate_admin_config()
    with mock.patch.object(get_settings(), "admin_password", ""), pytest.raises(RuntimeError):
        auth.validate_admin_config()
    assert any("Klartext" in w for w in auth.validate_admin_config())


def test_hash_from_b64_env():
    from app import config

    h = PasswordHasher().hash("pw")
    env = {"ADMIN_PASSWORD_HASH_B64": base64.b64encode(h.encode()).decode(), "ADMIN_PASSWORD": ""}
    with mock.patch.dict("os.environ", env):
        config.get_settings.cache_clear()
        try:
            s = config.get_settings()
            assert s.admin_password_hash == h and s.admin_password == ""
        finally:
            config.get_settings.cache_clear()
    get_settings()  # back to the test settings


def test_totp_second_step(client, db_data):
    secret = pyotp.random_base32()
    ui = _ui()
    with mock.patch.object(get_settings(), "admin_totp_secret", secret):
        r = _login(ui)
        assert r.status_code == 303 and r.headers["location"] == "/login/totp"
        assert auth.PRE_COOKIE in r.cookies and auth.COOKIE not in r.cookies
        # no session yet: the app is still locked
        assert ui.get("/", follow_redirects=False).headers.get("location") == "/login"
        assert ui.get("/login/totp").status_code == 200
        r = ui.post("/login/totp", data={"totp": "000000"}, follow_redirects=False)
        assert r.status_code == 401 and auth.COOKIE not in r.cookies
        r = ui.post("/login/totp", data={"totp": pyotp.TOTP(secret).now()}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/" and auth.COOKIE in r.cookies
        assert ui.get("/", follow_redirects=False).status_code == 200
    # without a pre-session the second step goes back to the login form
    assert _ui().post("/login/totp", data={"totp": "123456"}, follow_redirects=False).headers["location"] == "/login"


# ---------------------------------------------------------------- logout, redirects, links
def test_logout_needs_csrf_token(client, db_data):
    ui = _ui()
    _login(ui)
    assert ui.post("/logout", follow_redirects=False).status_code == 403
    assert ui.get("/", follow_redirects=False).status_code == 200  # still logged in
    csrf = re.search(r'name="csrf" value="([^"]+)"', ui.get("/").text).group(1)
    r = ui.post("/logout", data={"csrf": csrf}, headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    assert ui.get("/", follow_redirects=False).headers.get("location") == "/login"


def test_safe_next_rejects_open_redirects():
    assert auth.safe_next("/review?tenant=1") == "/review?tenant=1"
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "review", "", None, "/x\r\nSet-Cookie: a=b"):
        assert auth.safe_next(bad, "/review") == "/review"


def test_decide_back_is_limited_to_relative_paths(client, db_data):
    with SessionLocal() as db:
        lead = Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="X", email="x@kunde.example", status="needs_review")
        db.add(lead)
        db.flush()
        m = Message(lead_id=lead.id, tenant_id=db_data["alpha"], direction="out", step=0, subject="s", body="b", status="draft")
        db.add(m)
        db.commit()
        mid = m.id
    ui = _ui()
    _login(ui)
    csrf = re.search(r'name="csrf" value="([^"]+)"', ui.get("/review").text).group(1)
    r = ui.post(f"/messages/{mid}/decide", data={"csrf": csrf, "decision": "save", "back": "https://evil.example/"}, headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review"
    r = ui.post(f"/messages/{mid}/decide", data={"csrf": csrf, "decision": "save", "back": "/leads/1"}, headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.headers["location"] == "/leads/1"


def test_lead_website_is_only_linked_for_http(client, db_data):
    with SessionLocal() as db:
        a = Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="A", email="a@kunde.example", website="javascript:alert(1)")
        b = Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="B", email="b@kunde.example", website="https://b.example/")
        db.add_all([a, b])
        db.commit()
        ida, idb = a.id, b.id
    ui = _ui()
    _login(ui)
    assert 'href="javascript:' not in ui.get(f"/leads/{ida}").text
    assert 'href="https://b.example/"' in ui.get(f"/leads/{idb}").text


# ---------------------------------------------------------------- webhook scope
def test_webhook_only_suppresses_addresses_the_tenant_mailed(client, db_data):
    with SessionLocal() as db:
        sent = Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="S", email="sent@kunde.example", status="in_sequence", last_sent_at=datetime.now(timezone.utc))
        fresh = Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="F", email="fresh@kunde.example", status="enriched")
        db.add_all([sent, fresh])
        db.flush()
        db.add(Message(lead_id=sent.id, tenant_id=db_data["alpha"], direction="out", step=0, status="sent", provider_id="mail-1"))
        db.commit()
        tenant = db.get(Tenant, db_data["alpha"])
        # unknown address: nothing is blocked
        assert pipeline.handle_webhook(db, tenant, {"event": "unsubscribe", "rcpt": "victim@other.example"}) == "ignored"
        # a lead that was never mailed: nothing either
        assert pipeline.handle_webhook(db, tenant, {"event": "bounce", "rcpt": "fresh@kunde.example"}) == "ignored"
        # mailed lead, by address or by email id
        assert pipeline.handle_webhook(db, tenant, {"event": "bounce", "rcpt": "Sent@kunde.example"}).startswith("suppressed")
        db.commit()
        assert pipeline.handle_webhook(db, tenant, {"event": "spam", "email_id": "mail-1"}).startswith("suppressed")
        db.commit()
        values = {s.value for s in db.query(Suppression).all()}
        assert values == {"sent@kunde.example"}
        assert db.get(Lead, fresh.id).status == "enriched"
