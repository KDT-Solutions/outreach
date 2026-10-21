import re
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app as ui_app
from app.models import ApiAudit, Lead, Message
from app.services import smtp2go
from tests.conftest import AUTH


def _lead_with(db, tenant_id, campaign_id, email, status="draft", provider_id=None):
    lead = Lead(tenant_id=tenant_id, campaign_id=campaign_id, company=email.split("@")[1], email=email,
                status="needs_review" if status == "draft" else "in_sequence")
    db.add(lead)
    db.flush()
    m = Message(lead_id=lead.id, tenant_id=tenant_id, direction="out", step=0, subject="s", body="b", status=status,
                provider_id=provider_id, sent_at=datetime(2026, 10, 20, 7, 5, tzinfo=timezone.utc) if status == "sent" else None)
    db.add(m)
    db.flush()
    return lead, m


# ---------------------------------------------------------------- audit log
def test_ui_approval_lands_in_audit_log(client, db_data):
    with SessionLocal() as db:
        lead, m = _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "a@kunde.example")
        db.commit()
        mid = m.id
    ui = TestClient(ui_app, base_url="https://testserver")
    ui.post("/login", data={"username": "admin", "password": "test"}, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', ui.get("/review").text).group(1)
    r = ui.post(f"/messages/{mid}/decide", data={"csrf": csrf, "decision": "approve"}, headers={"origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    entries = client.get(f"/api/v1/audit?campaign_id={db_data['campaign_alpha']}", headers=AUTH).json()["entries"]
    assert entries[0]["tool"] == "ui_draft_approve" and entries[0]["actor"] == "ui:admin" and entries[0]["transport"] == "ui"
    assert entries[0]["object_ids"]["draft_id"] == mid and entries[0]["tenant"] == "alpha"


def test_audit_log_is_tenant_scoped_and_filterable(client, db_data):
    with SessionLocal() as db:
        db.add(ApiAudit(token_id="ui:admin", tenant_id=db_data["beta"], tool="ui_draft_approve", transport="ui", outcome="applied", object_ids={"campaign_id": db_data["campaign_beta"]}))
        db.add(ApiAudit(token_id="tok_x", tenant_id=db_data["alpha"], tool="approve_drafts", transport="mcp", outcome="applied", object_ids={"campaign_id": db_data["campaign_alpha"]}))
        db.commit()
    r = client.get("/api/v1/audit", headers=AUTH).json()
    assert [e["tool"] for e in r["entries"]] == ["approve_drafts"]  # beta's entry is not visible
    assert client.get("/api/v1/audit?tool=ui_draft_approve", headers=AUTH).json()["entries"] == []
    assert client.get("/api/v1/audit?tenant=beta", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/audit?campaign_id={db_data['campaign_beta']}", headers=AUTH).status_code == 404
    assert client.get("/api/v1/audit?since=kein-datum", headers=AUTH).status_code == 422


# ---------------------------------------------------------------- delivery status
EVENTS = [
    {"email_id": "id-1", "event": "processed", "date": "2026-10-20T07:05:01Z"},
    {"email_id": "id-1", "event": "delivered", "date": "2026-10-20T07:05:03Z"},
    {"email_id": "id-1", "event": "opened", "date": "2026-10-20T08:00:00Z"},
    {"email_id": "id-2", "event": "processed", "date": "2026-10-20T07:05:01Z"},
    {"email_id": "id-2", "event": "hard-bounced", "date": "2026-10-20T07:05:09Z", "smtp_response": "550 5.1.1 user unknown"},
]


def test_delivery_status_from_smtp2go(client, db_data):
    with SessionLocal() as db:
        _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "a@kunde.example", "sent", "id-1")
        _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "b@kunde.example", "sent", "id-2")
        _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "c@kunde.example", "sent", "id-3")
        _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "d@kunde.example", "draft")
        db.commit()

    def fake_search(api_key, start, sender=None, email_id=None, max_pages=5):
        assert api_key == "smtp-secret-key" and start == "2026-10-20T00:00:00Z"
        if sender:
            assert sender == "info@alpha.example"
            return EVENTS
        return []  # id-3 is not known to SMTP2GO

    with mock.patch.object(smtp2go, "activity_search", side_effect=fake_search) as search:
        r = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/delivery", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 3 and body["by_status"] == {"delivered": 1, "hard-bounced": 1, "unknown": 1}
    mails = {m["email"]: m for m in body["mails"]}
    assert mails["a@kunde.example"]["status"] == "delivered" and mails["a@kunde.example"]["opened"] is True
    assert mails["b@kunde.example"]["status"] == "hard-bounced" and "user unknown" in mails["b@kunde.example"]["reason"]
    assert mails["c@kunde.example"]["status"] == "unknown"
    assert search.call_count == 2  # one sender search, one lookup for the missing id
    assert "smtp-secret-key" not in r.text


def test_delivery_status_errors(client, db_data):
    with SessionLocal() as db:
        _lead_with(db, db_data["alpha"], db_data["campaign_alpha"], "a@kunde.example", "sent", "id-1")
        db.commit()
    with mock.patch.object(smtp2go, "activity_search", side_effect=smtp2go.ActivityError("SMTP2GO activity 401: bad key smtp-secret-key")):
        r = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/delivery", headers=AUTH)
    assert r.status_code == 502 and "smtp-secret-key" not in r.text
    assert client.get(f"/api/v1/campaigns/{db_data['campaign_beta']}/delivery", headers=AUTH).status_code == 404
