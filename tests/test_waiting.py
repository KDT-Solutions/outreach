"""Status "waiting" for campaigns and leads: parked until a condition is met, drafts hidden
from the review queue and blocked from approval, released back to where they were."""

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import pipeline
from app.db import SessionLocal
from app.main import app as ui_app
from app.models import ApiAudit, Campaign, Lead, Message
from tests.conftest import AUTH
from tests.test_mcp import call

STEPS = [{"subject": "Frage an {{firma}}", "body_text": "{{anrede}}\n\nText.", "delay_days": 0}]
ORIGIN = {"origin": "https://testserver"}


def _with_drafts(client, cid, n=2):
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    leads = [{"firma": f"F{i}", "anrede": "Guten Tag", "email": f"l{i}@firma{i}.ch"} for i in range(n)]
    r = client.post(f"/api/v1/campaigns/{cid}/leads/import", json={"leads": leads, "confirm": True}, headers=AUTH)
    ids = [c["lead_id"] for c in r.json()["created"]]
    with SessionLocal() as db:
        db.get(Campaign, cid).status = "active"
        for lead in db.query(Lead).filter(Lead.id.in_(ids)).all():
            lead.status = "needs_review"
            db.add(Message(lead_id=lead.id, tenant_id=lead.tenant_id, direction="out", step=0, subject=f"Frage an {lead.company}", body="Text", status="draft"))
        db.commit()
    return ids


def _lead(lead_id):
    with SessionLocal() as db:
        l = db.get(Lead, lead_id)
        return l.status, l.wait_reason, l.wait_prev_status, [m.status for m in l.messages]


def _ui():
    ui = TestClient(ui_app, base_url="https://testserver")
    ui.post("/login", data={"username": "admin", "password": "test"}, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', ui.get("/suppressions").text).group(1)
    return ui, csrf


# ---------------------------------------------------------------- leads
def test_wait_leads_preview_then_park(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    url = f"/api/v1/campaigns/{cid}/leads/wait"
    r = client.post(url, json={"lead_ids": [a], "reason": "wartet auf Unterlagen"}, headers=AUTH).json()
    assert r["confirmed"] is False and [x["lead_id"] for x in r["preview"]["would_park"]] == [a]
    assert _lead(a)[0] == "needs_review"

    r = client.post(url, json={"lead_ids": [a], "reason": "wartet auf Unterlagen", "confirm": True}, headers=AUTH).json()
    assert r["parked"] == [a]
    assert _lead(a) == ("waiting", "wartet auf Unterlagen", "needs_review", ["draft"])  # draft kept, not discarded
    lead = client.get(f"/api/v1/campaigns/{cid}/leads?status=waiting", headers=AUTH).json()["leads"][0]
    assert lead["wait_reason"] == "wartet auf Unterlagen" and lead["status_after_release"] == "needs_review"

    # hidden from list_drafts and skipped by approve_drafts all=true
    d = client.get(f"/api/v1/campaigns/{cid}/drafts", headers=AUTH).json()
    assert d["total"] == 1 and d["parked_waiting"] == 1 and d["drafts"][0]["lead"]["id"] == b
    assert client.get(f"/api/v1/campaigns/{cid}/drafts?include_waiting=true", headers=AUTH).json()["total"] == 2
    r = client.post(f"/api/v1/campaigns/{cid}/drafts/approve", json={"all": True, "confirm": True}, headers=AUTH).json()
    assert len(r["approved"]) == 1 and r["leads_waiting"] == [a]
    r = client.post(f"/api/v1/campaigns/{cid}/drafts/approve", json={"lead_ids": [a], "confirm": True}, headers=AUTH).json()
    assert r["approved"] == [] and r["leads_waiting"] == [a] and r["leads_without_draft"] == []
    assert _lead(a)[3] == ["draft"]

    stats = client.get(f"/api/v1/campaigns/{cid}/stats", headers=AUTH).json()
    assert stats["drafts_waiting_for_approval"] == 0 and stats["drafts_parked_waiting"] == 1


def test_release_leads_restores_previous_status(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    client.post(f"/api/v1/campaigns/{cid}/leads/wait", json={"all": True, "reason": "Partner zuerst", "confirm": True}, headers=AUTH)
    assert _lead(a)[0] == _lead(b)[0] == "waiting"
    url = f"/api/v1/campaigns/{cid}/leads/release"
    r = client.post(url, json={"lead_ids": [a]}, headers=AUTH).json()
    assert r["confirmed"] is False and r["preview"]["would_release"][0]["status_after"] == "needs_review"
    r = client.post(url, json={"lead_ids": [a], "confirm": True}, headers=AUTH).json()
    assert r["released"] == [{"lead_id": a, "status": "needs_review"}]
    assert _lead(a) == ("needs_review", None, None, ["draft"])
    assert _lead(b)[0] == "waiting"
    # releasing a lead that is not waiting changes nothing
    assert client.post(url, json={"lead_ids": [a], "confirm": True}, headers=AUTH).json()["released"] == []


def test_wait_leads_leaves_final_statuses_alone(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    with SessionLocal() as db:
        db.get(Lead, b).status = "replied"
        db.commit()
    r = client.post(f"/api/v1/campaigns/{cid}/leads/wait", json={"lead_ids": [a, b], "reason": "x", "confirm": True}, headers=AUTH).json()
    assert r["parked"] == [a] and r["not_parkable"] == [{"lead_id": b, "status": "replied"}]
    assert _lead(b)[0] == "replied"


def test_wait_leads_validation_and_scope(client, db_data):
    cid = db_data["campaign_alpha"]
    (a,) = _with_drafts(client, cid, 1)
    url = f"/api/v1/campaigns/{cid}/leads/wait"
    assert client.post(url, json={"lead_ids": [a], "confirm": True}, headers=AUTH).status_code == 422  # reason missing
    assert client.post(url, json={"lead_ids": [a], "reason": "", "confirm": True}, headers=AUTH).status_code == 422
    assert client.post(url, json={"lead_ids": [a], "all": True, "reason": "x"}, headers=AUTH).status_code == 422
    assert client.post(url, json={"lead_ids": [a + 999], "reason": "x"}, headers=AUTH).status_code == 404
    cb = db_data["campaign_beta"]
    assert client.post(f"/api/v1/campaigns/{cb}/leads/wait", json={"all": True, "reason": "x", "confirm": True}, headers=AUTH).status_code == 404


def test_waiting_lead_is_not_drafted_or_sent(client, db_data):
    cid = db_data["campaign_alpha"]
    (a,) = _with_drafts(client, cid, 1)
    client.post(f"/api/v1/campaigns/{cid}/drafts/approve", json={"all": True, "confirm": True}, headers=AUTH)
    with SessionLocal() as db:
        lead = db.get(Lead, a)
        lead.next_send_at = datetime.now(timezone.utc) - timedelta(hours=1)
        assert pipeline.set_lead_waiting(db, lead, "Stopp")
        db.commit()
    assert _lead(a) == ("waiting", "Stopp", "scheduled", ["approved"])
    with SessionLocal() as db:
        assert pipeline.generate_batch(db, 10) == 0
        # send_due only takes scheduled/in_sequence leads
        due = db.query(Message).join(Lead).filter(Message.status == "approved", Lead.status.in_(("scheduled", "in_sequence"))).count()
        assert due == 0
        lead = db.get(Lead, a)
        assert pipeline.release_lead(db, lead) == "scheduled"
        assert lead.next_send_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)  # past time moved into the next window
        db.commit()


def test_update_lead_keeps_waiting(client, db_data):
    cid = db_data["campaign_alpha"]
    (a,) = _with_drafts(client, cid, 1)
    client.post(f"/api/v1/campaigns/{cid}/leads/wait", json={"lead_ids": [a], "reason": "x", "confirm": True}, headers=AUTH)
    r = client.patch(f"/api/v1/leads/{a}", json={"email": "neu@firma0.ch", "confirm": True}, headers=AUTH)
    assert r.status_code == 200
    status, _, prev, msgs = _lead(a)
    assert status == "waiting" and prev == "new" and msgs == ["failed"]


# ---------------------------------------------------------------- campaigns
def test_wait_campaign_and_release_with_start(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    url = f"/api/v1/campaigns/{cid}/wait"
    r = client.post(url, json={"reason": "wartet auf Partner-Umfrage"}, headers=AUTH).json()
    assert r["confirmed"] is False and r["preview"]["drafts_hidden_from_review"] == 2
    r = client.post(url, json={"reason": "wartet auf Partner-Umfrage", "confirm": True}, headers=AUTH).json()
    assert r["campaign"]["status"] == "waiting" and r["campaign"]["wait_reason"] == "wartet auf Partner-Umfrage"

    d = client.get(f"/api/v1/campaigns/{cid}/drafts", headers=AUTH).json()
    assert d["total"] == 0 and d["parked_waiting"] == 2
    r = client.post(f"/api/v1/campaigns/{cid}/drafts/approve", json={"all": True, "confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "campaign_waiting"
    assert _lead(a)[3] == ["draft"]

    start = f"/api/v1/campaigns/{cid}/start"
    p = client.post(start, json={}, headers=AUTH).json()["preview"]
    assert p["wait_reason_cleared"] == "wartet auf Partner-Umfrage"
    r = client.post(start, json={"confirm": True, "confirm_send": True}, headers=AUTH).json()
    assert r["previous_status"] == "waiting" and r["drafts_released"] == 0
    with SessionLocal() as db:
        c = db.get(Campaign, cid)
        assert c.status == "active" and c.wait_reason is None
    assert client.get(f"/api/v1/campaigns/{cid}/drafts", headers=AUTH).json()["total"] == 2
    with SessionLocal() as db:
        tools = [x.tool for x in db.query(ApiAudit).filter(ApiAudit.outcome == "applied").order_by(ApiAudit.id)]
    assert "wait_campaign" in tools and "start_campaign" in tools


def test_wait_campaign_needs_reason_and_not_done(client, db_data):
    cid = db_data["campaign_alpha"]
    assert client.post(f"/api/v1/campaigns/{cid}/wait", json={"confirm": True}, headers=AUTH).status_code == 422
    with SessionLocal() as db:
        db.get(Campaign, cid).status = "done"
        db.commit()
    r = client.post(f"/api/v1/campaigns/{cid}/wait", json={"reason": "x", "confirm": True}, headers=AUTH)
    assert r.status_code == 409


def test_pause_clears_wait_reason(client, db_data):
    cid = db_data["campaign_alpha"]
    client.post(f"/api/v1/campaigns/{cid}/wait", json={"reason": "x", "confirm": True}, headers=AUTH)
    client.post(f"/api/v1/campaigns/{cid}/pause", json={"confirm": True}, headers=AUTH)
    with SessionLocal() as db:
        c = db.get(Campaign, cid)
        assert c.status == "paused" and c.wait_reason is None


def test_waiting_campaign_is_not_worked_on(client, db_data):
    cid = db_data["campaign_alpha"]
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    client.post(f"/api/v1/campaigns/{cid}/leads/import", json={"leads": [{"firma": "A", "anrede": "Hallo", "email": "a@a-firma.ch"}], "confirm": True}, headers=AUTH)
    with SessionLocal() as db:
        db.query(Lead).update({"status": "enriched"})
        db.get(Campaign, cid).status = "waiting"
        db.commit()
        assert pipeline.generate_batch(db, 10) == 0


# ---------------------------------------------------------------- web UI
def test_review_page_hides_waiting_drafts(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    client.post(f"/api/v1/campaigns/{cid}/leads/wait", json={"lead_ids": [a], "reason": "x", "confirm": True}, headers=AUTH)
    ui, csrf = _ui()
    page = ui.get("/review").text
    assert "F1" in page and ">F0<" not in page
    assert "1 offen" in page and "1 wartend" in page

    with SessionLocal() as db:
        mid = db.get(Lead, a).messages[0].id
    r = ui.post(f"/messages/{mid}/decide", data={"csrf": csrf, "decision": "approve"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 409
    assert _lead(a)[3] == ["draft"]

    # waiting campaign: nothing left in the queue
    ui.post(f"/campaigns/{cid}/status", data={"csrf": csrf, "status": "waiting", "wait_reason": "Händler später"}, headers=ORIGIN, follow_redirects=False)
    page = ui.get("/review").text
    assert "0 offen" in page and "2 wartend" in page
    assert "Händler später" in ui.get(f"/campaigns/{cid}").text


def test_ui_campaign_waiting_needs_reason(client, db_data):
    cid = db_data["campaign_alpha"]
    ui, csrf = _ui()
    r = ui.post(f"/campaigns/{cid}/status", data={"csrf": csrf, "status": "waiting"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.get(Campaign, cid).status != "waiting"


def test_ui_lead_wait_and_release(client, db_data):
    cid = db_data["campaign_alpha"]
    (a,) = _with_drafts(client, cid, 1)
    ui, csrf = _ui()
    r = ui.post(f"/leads/{a}/action", data={"csrf": csrf, "action": "wait", "wait_reason": "Unterlagen  fehlen"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert _lead(a)[:3] == ("waiting", "Unterlagen fehlen", "needs_review")
    page = ui.get(f"/leads/{a}").text
    assert "wartend" in page and "Unterlagen fehlen" in page and "Freigabe gesperrt" in page
    r = ui.post(f"/leads/{a}/action", data={"csrf": csrf, "action": "generate"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 409
    ui.post(f"/leads/{a}/action", data={"csrf": csrf, "action": "release"}, headers=ORIGIN, follow_redirects=False)
    assert _lead(a)[:3] == ("needs_review", None, None)


# ---------------------------------------------------------------- MCP
def test_mcp_wait_and_release(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    err, body = call(client, "wait_leads", campaign_id=cid, lead_ids=[b], reason="später", confirm=True)
    assert not err and body["parked"] == [b]
    err, body = call(client, "list_drafts", campaign_id=cid)
    assert body["total"] == 1 and body["parked_waiting"] == 1
    err, body = call(client, "release_leads", campaign_id=cid, all=True, confirm=True)
    assert body["released"] == [{"lead_id": b, "status": "needs_review"}]
    err, body = call(client, "wait_campaign", campaign_id=cid, reason="Plan Phase 2", confirm=True)
    assert not err and body["campaign"]["status"] == "waiting"
    err, body = call(client, "approve_drafts", campaign_id=cid, all=True, confirm=True)
    assert err and body["error"]["code"] == "campaign_waiting"
