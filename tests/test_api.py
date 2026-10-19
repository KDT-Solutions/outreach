from unittest import mock

from app.db import SessionLocal
from app.models import ApiAudit, Campaign, Lead, Message
from tests.conftest import AUTH, TOKEN

LEADS = [
    {"firma": "Garage Muster", "anrede": "Guten Tag Herr Muster", "vorname": "Hans", "nachname": "Muster", "email": "hans@garage-muster.ch", "ort": "Basel", "sprache": "de"},
    {"firma": "Beck AG", "vorname": "Anna", "nachname": "Beck", "email": "Anna@Beck.ch", "ort": "Bern"},
]
STEPS = [
    {"subject": "Frage an {{firma}}", "body_text": "{{anrede}}\n\nkurze Frage zu {{firma}} in {{ort}}.", "delay_days": 0},
    {"subject": "Nachfrage", "body_text": "Hallo {{vorname}}, kurz nachgefragt.", "delay_days": 4},
]


def _audit():
    with SessionLocal() as db:
        return db.query(ApiAudit).order_by(ApiAudit.id).all()


def _import(client, cid, leads=LEADS, confirm=True):
    return client.post(f"/api/v1/campaigns/{cid}/leads/import", json={"leads": leads, "confirm": confirm}, headers=AUTH)


# ---------------------------------------------------------------- auth
def test_missing_token_is_401_without_details(client):
    r = client.get("/api/v1/tenants")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": "Unauthorized"}}


def test_wrong_token_is_401(client):
    for header in (f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", TOKEN, "Basic abc"):
        r = client.get("/api/v1/tenants", headers={"Authorization": header})
        assert r.status_code == 401, header


def test_openapi_and_mcp_need_token(client):
    assert client.get("/api/v1/openapi.json").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    r = client.get("/api/v1/openapi.json", headers=AUTH)
    assert r.status_code == 200
    assert "/api/v1/campaigns/{campaign_id}/leads/import" in r.json()["paths"]


def test_plain_http_rejected(client):
    from fastapi.testclient import TestClient

    from app.api_server import app

    r = TestClient(app, base_url="http://testserver").get("/api/v1/tenants", headers=AUTH)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "https_required"


def test_ip_allowlist(client):
    from app.apiv1 import security

    security._allowed_networks.cache_clear()
    with mock.patch.object(security.get_settings(), "outreach_api_allow_ips", "10.0.0.0/8"):
        assert client.get("/api/v1/tenants", headers=AUTH).status_code == 403
    security._allowed_networks.cache_clear()
    with mock.patch.object(security.get_settings(), "outreach_api_allow_ips", "127.0.0.1, 10.0.0.1"):
        # TestClient's peer is "testclient", not an IP -> refused
        assert client.get("/api/v1/tenants", headers=AUTH).status_code == 403
    security._allowed_networks.cache_clear()


def test_rate_limit(client):
    from app.apiv1 import security

    with mock.patch.object(security.get_settings(), "outreach_api_rate_limit", 3):
        codes = [client.get("/api/v1/tenants", headers=AUTH).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_only_assigned_tenants_listed(client):
    r = client.get("/api/v1/tenants", headers=AUTH)
    assert [t["slug"] for t in r.json()["tenants"]] == ["alpha"]
    r = client.get("/api/v1/campaigns", headers=AUTH)
    assert [c["name"] for c in r.json()["campaigns"]] == ["Alpha Herbst"]


def test_foreign_tenant_is_404(client, db_data):
    cb = db_data["campaign_beta"]
    assert client.get(f"/api/v1/campaigns/{cb}", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/campaigns/{cb}/leads", headers=AUTH).status_code == 404
    assert client.get("/api/v1/campaigns?tenant=beta", headers=AUTH).status_code == 404
    r = _import(client, cb)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = client.post(f"/api/v1/campaigns/{cb}/start", json={"confirm": True, "confirm_send": True}, headers=AUTH)
    assert r.status_code == 404
    # same answer as a campaign that does not exist
    assert client.get("/api/v1/campaigns/9999", headers=AUTH).json() == client.get(f"/api/v1/campaigns/{cb}", headers=AUTH).json()
    # a lead of the foreign tenant
    with SessionLocal() as db:
        lead = Lead(tenant_id=db_data["beta"], campaign_id=cb, company="X", email="x@y.ch")
        db.add(lead)
        db.commit()
        lid = lead.id
    assert client.patch(f"/api/v1/leads/{lid}", json={"ort": "Chur", "confirm": True}, headers=AUTH).status_code == 404
    assert client.delete(f"/api/v1/leads/{lid}?confirm=true", headers=AUTH).status_code == 404
    with SessionLocal() as db:
        assert db.get(Lead, lid).city is None and db.get(Lead, lid).deleted_at is None


def test_create_campaign_for_foreign_tenant_is_404(client):
    body = {"tenant": "beta", "name": "X", "sender_name": "A", "sender_email": "a@alpha.example", "reply_to": "r@alpha.example", "confirm": True}
    assert client.post("/api/v1/campaigns", json=body, headers=AUTH).status_code == 404


# ---------------------------------------------------------------- import
def test_import_preview_changes_nothing(client, db_data):
    r = _import(client, db_data["campaign_alpha"], confirm=False)
    assert r.status_code == 200
    body = r.json()
    assert body["confirmed"] is False
    assert body["preview"]["to_create"] == 2
    with SessionLocal() as db:
        assert db.query(Lead).count() == 0


def test_import_detects_duplicates(client, db_data):
    cid = db_data["campaign_alpha"]
    r = _import(client, cid)
    assert r.status_code == 200
    assert len(r.json()["created"]) == 2
    # second run: both exist; plus a duplicate inside the request (case-insensitive)
    again = LEADS + [{"firma": "Neu GmbH", "email": "neu@neu.ch"}, {"firma": "Neu GmbH 2", "email": "NEU@neu.ch"}]
    r = _import(client, cid, again)
    body = r.json()
    assert [c["email"] for c in body["created"]] == ["neu@neu.ch"]
    reasons = {(s["email"], s["reason"]) for s in body["skipped"]}
    assert reasons == {("hans@garage-muster.ch", "exists"), ("anna@beck.ch", "exists"), ("neu@neu.ch", "duplicate_in_request")}
    with SessionLocal() as db:
        assert db.query(Lead).filter(Lead.campaign_id == cid).count() == 3
        stored = db.query(Lead).filter(Lead.email == "anna@beck.ch").one()
        assert (stored.first_name, stored.last_name, stored.city, stored.email_source, stored.status) == ("Anna", "Beck", "Bern", "import", "new")


def test_import_skips_suppressed(client, db_data):
    from app.models import Suppression

    with SessionLocal() as db:
        db.add(Suppression(tenant_id=db_data["alpha"], value="@beck.ch", reason="manual"))
        db.commit()
    body = _import(client, db_data["campaign_alpha"]).json()
    assert [c["email"] for c in body["created"]] == ["hans@garage-muster.ch"]
    assert body["skipped"][0]["reason"] == "suppressed"


def test_import_validation_is_structured(client, db_data):
    bad = [{"firma": "A", "email": "kein-mail"}, {"firma": "", "email": "a@b.ch"}, {"firma": "C", "email": "c@d.ch", "foo": 1}]
    r = _import(client, db_data["campaign_alpha"], bad)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    locs = {tuple(d["loc"][-3:]) for d in err["details"]}
    assert ("leads", 0, "email") in locs and ("leads", 1, "firma") in locs and ("leads", 2, "foo") in locs
    with SessionLocal() as db:
        assert db.query(Lead).count() == 0


# ---------------------------------------------------------------- sequence and preview
def test_sequence_validation(client, db_data):
    cid = db_data["campaign_alpha"]
    cases = [
        [{"subject": "Hi {{name}}", "body_text": "x", "delay_days": 0}],
        [{"subject": "Hi", "body_text": "<p>x</p>", "delay_days": 0}],
        [{"subject": "Hi", "body_text": "x {{firma", "delay_days": 0}],
        [{"subject": "Hi", "body_text": "x", "delay_days": 3}],
    ]
    for steps in cases:
        r = client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": steps, "confirm": True}, headers=AUTH)
        assert r.status_code == 422, steps
        assert r.json()["error"]["details"]


def test_sequence_preview_and_render(client, db_data):
    cid = db_data["campaign_alpha"]
    r = client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS}, headers=AUTH)
    assert r.json()["confirmed"] is False
    with SessionLocal() as db:
        assert db.get(Campaign, cid).steps == []
    r = client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    assert r.json()["confirmed"] is True
    lead_id = _import(client, cid).json()["created"][0]["lead_id"]
    mail = client.get(f"/api/v1/campaigns/{cid}/preview?lead_id={lead_id}&step=1", headers=AUTH).json()
    assert mail["subject"] == "Frage an Garage Muster"
    assert mail["body_text"].startswith("Guten Tag Herr Muster\n\nkurze Frage zu Garage Muster in Basel.")
    assert "Alpha AG, Bahnhofstrasse 1" in mail["body_text"] and "/u/" in mail["body_text"]
    assert mail["to"] == "hans@garage-muster.ch"
    assert mail["missing_placeholders"] == []
    other = _import(client, cid, [{"firma": "Ohne Name", "email": "x@ohne.ch"}]).json()["created"][0]["lead_id"]
    mail = client.get(f"/api/v1/campaigns/{cid}/preview?lead_id={other}&step=1", headers=AUTH).json()
    assert set(mail["missing_placeholders"]) == {"anrede", "ort"}
    assert client.get(f"/api/v1/campaigns/{cid}/preview?lead_id={lead_id}&step=3", headers=AUTH).status_code == 422
    with SessionLocal() as db:
        assert db.query(Message).count() == 0


def test_worker_renders_template_and_holds_incomplete(client, db_data):
    from app import pipeline

    cid = db_data["campaign_alpha"]
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    _import(client, cid, LEADS + [{"firma": "Ohne Name", "email": "x@ohne.ch"}])
    with SessionLocal() as db:
        c = db.get(Campaign, cid)
        c.auto_approve = True
        c.status = "active"
        db.commit()
        with mock.patch("app.services.verify.check", return_value=True):
            pipeline.enrich_batch(db, 10)
        pipeline.generate_batch(db, 10)
        by_email = {l.email: l for l in db.query(Lead).all()}
        assert by_email["hans@garage-muster.ch"].status == "scheduled"
        assert by_email["hans@garage-muster.ch"].messages[0].subject == "Frage an Garage Muster"
        # empty placeholders: never auto-approved
        assert by_email["x@ohne.ch"].status == "needs_review"


# ---------------------------------------------------------------- confirm logic
def test_write_tools_preview_without_confirm(client, db_data):
    cid = db_data["campaign_alpha"]
    lead_id = _import(client, cid).json()["created"][0]["lead_id"]
    n_audit = len(_audit())
    body = {"name": "Neu", "sender_name": "Alpha Vertrieb", "sender_email": "vertrieb@alpha.example", "reply_to": "antwort@alpha.example"}
    assert client.post("/api/v1/campaigns", json=body, headers=AUTH).json()["confirmed"] is False
    assert client.patch(f"/api/v1/leads/{lead_id}", json={"ort": "Chur"}, headers=AUTH).json()["confirmed"] is False
    assert client.delete(f"/api/v1/leads/{lead_id}", headers=AUTH).json()["confirmed"] is False
    assert client.post(f"/api/v1/campaigns/{cid}/pause", json={}, headers=AUTH).json()["confirmed"] is False
    assert client.post(f"/api/v1/campaigns/{cid}/stop", json={}, headers=AUTH).json()["confirmed"] is False
    with SessionLocal() as db:
        assert db.query(Campaign).count() == 2
        lead = db.get(Lead, lead_id)
        assert lead.city == "Basel" and lead.deleted_at is None
        assert db.get(Campaign, cid).status == "draft"
    audit = _audit()[n_audit:]
    assert [a.outcome for a in audit] == ["preview"] * 5
    assert {a.tool for a in audit} == {"create_campaign", "update_lead", "delete_lead", "pause_campaign", "stop_campaign"}


def test_start_needs_confirm_and_confirm_send(client, db_data):
    cid = db_data["campaign_alpha"]
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    url = f"/api/v1/campaigns/{cid}/start"
    r = client.post(url, json={}, headers=AUTH)
    assert r.status_code == 200 and r.json()["confirmed"] is False
    r = client.post(url, json={"confirm_send": True}, headers=AUTH)
    assert r.json()["confirmed"] is False
    r = client.post(url, json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirm_send_required"
    with SessionLocal() as db:
        assert db.get(Campaign, cid).status == "draft"
    r = client.post(url, json={"confirm": True, "confirm_send": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["confirmed"] is True
    with SessionLocal() as db:
        assert db.get(Campaign, cid).status == "active"
    outcomes = [(a.tool, a.outcome) for a in _audit() if a.tool == "start_campaign"]
    assert outcomes == [("start_campaign", "preview"), ("start_campaign", "preview"), ("start_campaign", "error"), ("start_campaign", "applied")]


def test_start_refuses_without_sequence(client, db_data):
    r = client.post(f"/api/v1/campaigns/{db_data['campaign_alpha']}/start", json={"confirm": True, "confirm_send": True}, headers=AUTH)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_ready"


def test_create_update_delete_applied_and_audited(client, db_data):
    body = {"name": "Neu", "sender_name": "Alpha Vertrieb", "sender_email": "vertrieb@alpha.example", "reply_to": "antwort@alpha.example", "confirm": True}
    r = client.post("/api/v1/campaigns", json=body, headers=AUTH).json()
    cid = r["campaign"]["id"]
    assert r["campaign"]["sender_email"] == "vertrieb@alpha.example" and r["campaign"]["status"] == "draft"
    lead_id = _import(client, cid).json()["created"][0]["lead_id"]
    r = client.patch(f"/api/v1/leads/{lead_id}", json={"ort": "Chur", "confirm": True}, headers=AUTH).json()
    assert r["lead"]["ort"] == "Chur"
    r = client.delete(f"/api/v1/leads/{lead_id}?confirm=true", headers=AUTH).json()
    assert r["confirmed"] is True
    leads = client.get(f"/api/v1/campaigns/{cid}/leads", headers=AUTH).json()
    assert lead_id not in [l["id"] for l in leads["leads"]]
    with SessionLocal() as db:
        assert db.get(Lead, lead_id).deleted_at is not None  # soft delete, row stays
    applied = [a for a in _audit() if a.outcome == "applied"]
    assert [a.tool for a in applied] == ["create_campaign", "import_leads", "update_lead", "delete_lead"]
    assert all(a.tenant_id == db_data["alpha"] and a.token_id.startswith("tok_") and a.transport == "rest" for a in applied)
    assert applied[1].object_ids["campaign_id"] == cid and len(applied[1].object_ids["lead_ids"]) == 2
    assert applied[3].object_ids["lead_id"] == lead_id


def test_update_email_duplicate_refused(client, db_data):
    cid = db_data["campaign_alpha"]
    created = _import(client, cid).json()["created"]
    r = client.patch(f"/api/v1/leads/{created[0]['lead_id']}", json={"email": "anna@beck.ch", "confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate"


def test_send_test_domain_allowlist_and_confirm(client, db_data):
    cid = db_data["campaign_alpha"]
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    url = f"/api/v1/campaigns/{cid}/send-test"
    r = client.post(url, json={"to": "someone@kunde.ch", "confirm": True}, headers=AUTH)
    assert r.status_code == 403 and r.json()["error"]["code"] == "recipient_not_allowed"
    with mock.patch("app.services.smtp2go.send", return_value=("id1", "<m@x>")) as send:
        r = client.post(url, json={"to": "me@example.com"}, headers=AUTH)
        assert r.json()["confirmed"] is False
        assert r.json()["preview"]["would_send"]["subject"] == "[TEST] Frage an Muster AG"
        send.assert_not_called()
        r = client.post(url, json={"to": "me@example.com", "confirm": True}, headers=AUTH)
        assert r.json()["confirmed"] is True
        send.assert_called_once()
        assert send.call_args.kwargs["to"] == "me@example.com"
    assert "smtp-secret-key" not in r.text


def test_send_test_error_hides_key(client, db_data):
    from app.services import smtp2go

    cid = db_data["campaign_alpha"]
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    with mock.patch("app.services.smtp2go.send", side_effect=smtp2go.SendError("bad key smtp-secret-key")):
        r = client.post(f"/api/v1/campaigns/{cid}/send-test", json={"to": "me@example.com", "confirm": True}, headers=AUTH)
    assert r.status_code == 502
    assert "smtp-secret-key" not in r.text


def test_no_secrets_in_tenant_list(client):
    from app import crypto

    with SessionLocal() as db:
        from app.models import Tenant

        t = db.query(Tenant).filter_by(slug="alpha").one()
        t.smtp2go_api_key_enc = crypto.encrypt("tenant-smtp-key")
        t.zammad_url = "https://zammad.example"
        t.zammad_token_enc = crypto.encrypt("zammad-token")
        db.commit()
        webhook = t.webhook_token
    r = client.get("/api/v1/tenants", headers=AUTH)
    assert r.json()["tenants"][0]["smtp_configured"] is True
    for secret in ("tenant-smtp-key", "zammad-token", webhook, "smtp-secret-key"):
        assert secret not in r.text


def test_stats(client, db_data):
    cid = db_data["campaign_alpha"]
    created = _import(client, cid).json()["created"]
    with SessionLocal() as db:
        a, b = (db.get(Lead, c["lead_id"]) for c in created)
        for lead in (a, b):
            db.add(Message(lead_id=lead.id, tenant_id=lead.tenant_id, direction="out", status="sent", subject="s"))
        a.status = "replied"
        db.add(Message(lead_id=a.id, tenant_id=a.tenant_id, direction="in", status="received", classification="interested", zammad_ticket_id=5))
        db.add(Message(lead_id=b.id, tenant_id=b.tenant_id, direction="in", status="received", classification="out_of_office", zammad_ticket_id=6))
        b.status = "bounced"
        db.commit()
    s = client.get(f"/api/v1/campaigns/{cid}/stats", headers=AUTH).json()
    assert (s["sent"], s["bounces"], s["replies"], s["auto_replies"]) == (2, 1, 1, 1)
