"""Outcomes won / not_interested (set by the operator, e.g. after a call) and the lead search across campaigns."""

import re

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app as ui_app
from app.models import Lead, Message
from app.services import leadsearch
from tests.conftest import AUTH
from tests.test_mcp import call


def _lead(db_data, campaign="campaign_alpha", tenant="alpha", **kw):
    with SessionLocal() as db:
        lead = Lead(tenant_id=db_data[tenant], campaign_id=db_data[campaign], **kw)
        db.add(lead)
        db.commit()
        return lead.id


def _ui():
    ui = TestClient(ui_app, base_url="https://testserver")
    ui.post("/login", data={"username": "admin", "password": "test"}, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', ui.get("/suppressions").text).group(1)
    return ui, csrf


# ---------------------------------------------------------------- outcome
def test_won_stops_sequence_and_keeps_note(client, db_data):
    lid = _lead(db_data, company="H.S.K CLES SERVICES", email="info@hsk.example", status="in_sequence", step=1)
    with SessionLocal() as db:
        db.add(Message(lead_id=lid, tenant_id=db_data["alpha"], direction="out", step=1, subject="Relance", body="x", status="draft"))
        db.commit()
    url = f"/api/v1/leads/{lid}/outcome"
    r = client.post(url, json={"outcome": "won", "note": "Rückruf, Termin vereinbaren"}, headers=AUTH).json()
    assert r["confirmed"] is False and r["preview"]["pending_mails_discarded"] == 1
    r = client.post(url, json={"outcome": "won", "note": "Rückruf, Termin vereinbaren", "confirm": True}, headers=AUTH).json()
    assert r["lead"]["status"] == "won" and r["lead"]["outcome_note"] == "Rückruf, Termin vereinbaren" and r["pending_mails_discarded"] == 1
    with SessionLocal() as db:
        lead = db.get(Lead, lid)
        assert lead.next_send_at is None and lead.outcome_at is not None
        assert [m.status for m in lead.messages] == ["failed"]
    stats = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/stats", headers=AUTH).json()
    assert stats["won"] == 1 and stats["not_interested"] == 0


def test_not_interested_from_waiting_and_undo(client, db_data):
    lid = _lead(db_data, company="Parked", status="waiting", wait_reason="x", wait_prev_status="in_sequence", step=1)
    err, body = call(client, "set_lead_outcome", lead_id=lid, outcome="not_interested", note="am Telefon abgesagt", confirm=True)
    assert not err and body["lead"]["status"] == "not_interested"
    with SessionLocal() as db:
        lead = db.get(Lead, lid)
        assert lead.wait_reason is None and lead.wait_prev_status is None
    stats = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/stats", headers=AUTH).json()
    assert stats["not_interested"] == 1
    err, body = call(client, "clear_lead_outcome", lead_id=lid, confirm=True)
    assert not err and body["lead"]["status"] == "stopped"  # no reply: sequence is not resumed
    err, body = call(client, "clear_lead_outcome", lead_id=lid, confirm=True)
    assert err and body["error"]["code"] == "no_outcome"


def test_outcome_validation_and_scope(client, db_data):
    lid = _lead(db_data, company="Eigen", status="in_sequence")
    err, body = call(client, "set_lead_outcome", lead_id=lid, outcome="vielleicht", confirm=True)
    assert err and body["error"]["code"] == "validation_error"
    fid = _lead(db_data, campaign="campaign_beta", tenant="beta", company="Fremd", status="in_sequence")
    err, body = call(client, "set_lead_outcome", lead_id=fid, outcome="won", confirm=True)
    assert err and body["error"]["code"] == "not_found"


def test_reply_does_not_overwrite_outcome(client, db_data, monkeypatch):
    from app import pipeline
    from app.models import Tenant
    from app.services import llm, zammad
    from app import crypto

    lid = _lead(db_data, company="Regio", email="r@regio.example", status="not_interested", step=1)
    with SessionLocal() as db:
        t = db.get(Tenant, db_data["alpha"])
        t.zammad_url, t.zammad_token_enc = "https://zammad.example", crypto.encrypt("tok")
        lead = db.get(Lead, lid)
        from datetime import datetime, timezone
        lead.last_sent_at = datetime.now(timezone.utc)
        db.commit()
    monkeypatch.setattr(zammad, "recent_tickets", lambda *a, **k: [{"id": 77, "number": "1", "title": "Re: Frage", "customer": {"email": "r@regio.example"}}])
    monkeypatch.setattr(zammad, "first_article", lambda *a, **k: {"body": "Danke, wir melden uns."})
    monkeypatch.setattr(zammad, "add_note", lambda *a, **k: None)
    monkeypatch.setattr(llm, "classify_reply", lambda *a, **k: "interested")
    with SessionLocal() as db:
        pipeline.poll_replies(db)
    with SessionLocal() as db:
        lead = db.get(Lead, lid)
        assert lead.status == "not_interested" and [m.direction for m in lead.messages] == ["in"]


def test_ui_outcomes_and_dashboard(client, db_data):
    a = _lead(db_data, company="Klee Schlüssel", status="in_sequence")
    b = _lead(db_data, company="Absage AG", status="in_sequence")
    ui, csrf = _ui()
    r = ui.post(f"/leads/{a}/action", data={"csrf": csrf, "action": "won", "outcome_note": "Rückruf Hr. Klee"}, follow_redirects=False)
    assert r.status_code == 303
    page = ui.get(f"/leads/{a}").text
    assert "erfolgreich" in page and "Rückruf Hr. Klee" in page and "Ergebnis zurücknehmen" in page
    assert ui.post(f"/leads/{a}/action", data={"csrf": csrf, "action": "requeue"}).status_code == 409
    ui.post(f"/leads/{b}/action", data={"csrf": csrf, "action": "not_interested", "outcome_note": "am Telefon abgesagt"}, follow_redirects=False)
    assert "nicht interessiert" in ui.get(f"/leads/{b}").text
    dash = ui.get("/").text
    assert "Erfolgreich" in dash and "Nicht interessiert" in dash and 'name="q"' in dash
    assert "Absage AG" in ui.get("/leads", params={"status": "not_interested"}).text


# ---------------------------------------------------------------- search
def test_terms_phone_and_words():
    assert leadsearch.terms("+41 21 831 00 01") == [("phone", "218310001")]
    assert leadsearch.terms("021/831.00.01 Clés") == [("phone", "218310001"), ("word", "cles")]
    assert leadsearch.terms("H.S.K") == [("word", "hsk")]


def test_search_ui_finds_call_note_leads(client, db_data):
    a = _lead(db_data, company="H.S.K CLES SERVICES", city="Vallorbe", phone="021 831 00 01", email="info@h-s-k-24.com", status="in_sequence")
    b = _lead(db_data, company="Clés Barby SA", city="Lausanne", email="info@clesbarby.ch", status="scheduled")
    _lead(db_data, campaign="campaign_beta", tenant="beta", company="Andere Firma", phone="079 409 16 67")
    with SessionLocal() as db:
        ids = lambda q: [l.id for l in leadsearch.search(db, q)[0]]
        assert ids("HSK") == [a]
        assert ids("+41218310001") == [a]
        assert ids("0041 21 831 00 01") == [a]
        assert ids("cles") == [b, a]
        assert ids("vallorbe cles") == [a]
        rows, partial = leadsearch.search(db, "HSK - LES SERVICES CLES PLUS Sàrl")
        assert partial and rows[0].id == a
    ui, _ = _ui()
    page = ui.get("/leads", params={"q": "+41 21 831 00 01"}).text
    assert "H.S.K CLES SERVICES" in page and "Clés Barby" not in page


def test_find_leads_api_is_tenant_scoped(client, db_data):
    _lead(db_data, campaign="campaign_beta", tenant="beta", company="Geheim AG", phone="079 409 16 67")
    a = _lead(db_data, company="Offen AG", phone="+41 79 409 16 67")
    err, body = call(client, "find_leads", query="0794091667")
    assert not err and [l["id"] for l in body["leads"]] == [a] and body["leads"][0]["telefon"] == "+41 79 409 16 67"
    r = client.get("/api/v1/leads/search", params={"q": "Geheim"}, headers=AUTH).json()
    assert r["leads"] == []
