from app.db import SessionLocal
from app.models import ApiAudit, Campaign, Lead, Message, TenantSignature
from tests.conftest import AUTH

STEPS = [{"subject": "Frage an {{firma}}", "body_text": "{{anrede}}\n\nText.", "delay_days": 0}]
DE = {"body_text": "Freundliche Grüsse\n{{sender_name}}\nAlpha AG", "unsubscribe_text": "Hier abmelden:"}
FR = {"body_text": "Meilleures salutations\n{{sender_name}}\nAlpha SA", "unsubscribe_text": "Vous ne souhaitez plus recevoir nos e-mails ? Désinscription ici :"}


def _setup(client, cid, leads):
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "confirm": True}, headers=AUTH)
    r = client.post(f"/api/v1/campaigns/{cid}/leads/import", json={"leads": leads, "confirm": True}, headers=AUTH)
    return [c["lead_id"] for c in r.json()["created"]]


def _preview(client, cid, lead_id):
    return client.get(f"/api/v1/campaigns/{cid}/preview?lead_id={lead_id}&step=1", headers=AUTH).json()


def _set_sig(client, lang, body, confirm=True):
    return client.put(f"/api/v1/tenants/alpha/signatures/{lang}", json={**body, "confirm": confirm}, headers=AUTH)


# ---------------------------------------------------------------- signatures
def test_signature_resolution_all_four_levels(client, db_data):
    cid = db_data["campaign_alpha"]  # campaign language de
    fr_lead, de_lead, it_lead = _setup(client, cid, [
        {"firma": "Garage SA", "email": "a@garage-sa.ch", "sprache": "fr"},
        {"firma": "Garage AG", "email": "b@garage-ag.ch", "sprache": "de"},
        {"firma": "Garage SRL", "email": "c@garage-srl.ch", "sprache": "it"},
    ])

    # 4: nothing configured -> tenant default (signature + footer, German unsubscribe text)
    p = _preview(client, cid, fr_lead)
    assert p["signature"]["source"] == "default" and p["signature"]["unsubscribe_text_source"] == "default"
    assert "Alpha AG, Bahnhofstrasse 1" in p["body_text"]
    assert "Keine weiteren Mails von uns gewünscht? Hier abmelden: https://testserver/u/" in p["body_text"]

    # 3: only a German signature -> campaign language for the Italian lead
    assert _set_sig(client, "de", DE).json()["confirmed"] is True
    p = _preview(client, cid, it_lead)
    assert p["signature"]["source"] == "tenant:de"
    assert "Freundliche Grüsse\nAlpha\nAlpha AG" in p["body_text"]  # {{sender_name}} = tenant from_name
    assert "Hier abmelden: https://testserver/u/" in p["body_text"]

    # 2: French signature wins for the French lead, German lead stays German
    _set_sig(client, "fr", FR)
    p = _preview(client, cid, fr_lead)
    assert p["signature"]["source"] == "tenant:fr" and p["signature"]["unsubscribe_text_source"] == "tenant:fr"
    assert "Meilleures salutations" in p["body_text"]
    assert "Désinscription ici : https://testserver/u/" in p["body_text"]
    assert _preview(client, cid, de_lead)["signature"]["source"] == "tenant:de"

    # 1: campaign override beats everything; separate override for the unsubscribe text
    r = client.put(f"/api/v1/campaigns/{cid}/sequence", json={
        "steps": STEPS, "signature_override": "Beste Grüsse\n{{sender_name}} ({{reply_to}})\nAbmelden: {{unsubscribe_url}}", "confirm": True,
    }, headers=AUTH)
    assert r.status_code == 200
    p = _preview(client, cid, fr_lead)
    assert p["signature"]["source"] == "campaign"
    assert p["signature"]["unsubscribe_text_source"] == "tenant:fr"
    assert "Beste Grüsse\nAlpha (info@alpha.example)\nAbmelden: https://testserver/u/" in p["body_text"]
    # {{unsubscribe_url}} placed in the signature: no second unsubscribe line
    assert p["body_text"].count("/u/") == 1

    # clearing the override with "" falls back again
    client.put(f"/api/v1/campaigns/{cid}/sequence", json={"steps": STEPS, "signature_override": "", "confirm": True}, headers=AUTH)
    assert _preview(client, cid, fr_lead)["signature"]["source"] == "tenant:fr"


def test_unsubscribe_override_only(client, db_data):
    body = {"name": "FR", "sender_name": "Alpha", "sender_email": "a@alpha.example", "reply_to": "r@alpha.example",
            "language": "fr", "unsubscribe_text_override": "Plus de mails ? Cliquez ici :", "confirm": True}
    cid = client.post("/api/v1/campaigns", json=body, headers=AUTH).json()["campaign"]["id"]
    (lead,) = _setup(client, cid, [{"firma": "X", "email": "x@x-sa.ch"}])
    p = _preview(client, cid, lead)
    assert p["signature"]["source"] == "default" and p["signature"]["unsubscribe_text_source"] == "campaign"
    assert "Plus de mails ? Cliquez ici : https://testserver/u/" in p["body_text"]


def test_signature_preview_and_validation(client, db_data):
    r = _set_sig(client, "fr", FR, confirm=False)
    assert r.json()["confirmed"] is False and r.json()["preview"]["action"] == "create"
    with SessionLocal() as db:
        assert db.query(TenantSignature).count() == 0
    bad = _set_sig(client, "fr", {"body_text": "Gruss {{vorname}}", "unsubscribe_text": "x"})
    assert bad.status_code == 422 and "vorname" in str(bad.json()["error"]["details"])
    assert _set_sig(client, "fra", FR).status_code == 422
    _set_sig(client, "fr", FR)
    sigs = client.get("/api/v1/tenants/alpha/signatures", headers=AUTH).json()
    assert [s["language"] for s in sigs["signatures"]] == ["fr"]
    assert "Alpha AG, Bahnhofstrasse 1" in sigs["default"]["body_text"]
    with SessionLocal() as db:
        assert [a.outcome for a in db.query(ApiAudit).filter_by(tool="set_tenant_signature").order_by(ApiAudit.id)] == ["preview", "applied"]  # invalid input is rejected before the tool runs


def test_signature_foreign_tenant_404(client):
    assert client.get("/api/v1/tenants/beta/signatures", headers=AUTH).status_code == 404
    r = client.put("/api/v1/tenants/beta/signatures/fr", json={**FR, "confirm": True}, headers=AUTH)
    assert r.status_code == 404
    with SessionLocal() as db:
        assert db.query(TenantSignature).count() == 0


# ---------------------------------------------------------------- approval
def _with_drafts(client, cid, n=2):
    ids = _setup(client, cid, [{"firma": f"F{i}", "anrede": "Guten Tag", "email": f"l{i}@firma{i}.ch"} for i in range(n)])
    with SessionLocal() as db:
        for lead in db.query(Lead).filter(Lead.id.in_(ids)).all():
            lead.status = "needs_review"
            db.add(Message(lead_id=lead.id, tenant_id=lead.tenant_id, direction="out", step=0, subject=f"Frage an {lead.company}", body="Text", status="draft"))
        db.commit()
    return ids


def _statuses(ids):
    with SessionLocal() as db:
        return {l.id: (l.status, l.messages[0].status) for l in db.query(Lead).filter(Lead.id.in_(ids)).all()}


def test_list_drafts_rendered(client, db_data):
    cid = db_data["campaign_alpha"]
    ids = _with_drafts(client, cid)
    r = client.get(f"/api/v1/campaigns/{cid}/drafts", headers=AUTH).json()
    assert r["total"] == 2
    d = r["drafts"][0]
    assert d["step"] == 1 and d["lead"]["id"] in ids and d["subject"].startswith("Frage an F")
    assert d["body_text"].startswith("Text\n\n") and "/u/" in d["body_text"]


def test_approve_drafts_with_and_without_confirm(client, db_data):
    cid = db_data["campaign_alpha"]
    a, b = _with_drafts(client, cid)
    url = f"/api/v1/campaigns/{cid}/drafts/approve"
    r = client.post(url, json={"lead_ids": [a]}, headers=AUTH).json()
    assert r["confirmed"] is False and [x["lead_id"] for x in r["preview"]["would_approve"]] == [a]
    assert _statuses([a, b]) == {a: ("needs_review", "draft"), b: ("needs_review", "draft")}
    r = client.post(url, json={"lead_ids": [a], "confirm": True}, headers=AUTH).json()
    assert r["confirmed"] is True and len(r["approved"]) == 1
    st = _statuses([a, b])
    assert st[a] == ("scheduled", "approved") and st[b] == ("needs_review", "draft")
    # all=true takes the rest; a second run finds nothing
    assert len(client.post(url, json={"all": True, "confirm": True}, headers=AUTH).json()["approved"]) == 1
    assert client.post(url, json={"all": True, "confirm": True}, headers=AUTH).json()["approved"] == []
    with SessionLocal() as db:
        outcomes = [(x.outcome, x.tenant_id) for x in db.query(ApiAudit).filter_by(tool="approve_drafts").order_by(ApiAudit.id)]
    assert outcomes == [("preview", db_data["alpha"])] + [("applied", db_data["alpha"])] * 3


def test_approve_drafts_needs_exactly_one_selector(client, db_data):
    url = f"/api/v1/campaigns/{db_data['campaign_alpha']}/drafts/approve"
    assert client.post(url, json={"confirm": True}, headers=AUTH).status_code == 422
    assert client.post(url, json={"lead_ids": [1], "all": True, "confirm": True}, headers=AUTH).status_code == 422


def test_update_campaign_auto_approve_releases_drafts(client, db_data):
    cid = db_data["campaign_alpha"]
    ids = _with_drafts(client, cid)
    url = f"/api/v1/campaigns/{cid}"
    r = client.patch(url, json={"auto_approve": True, "name": "Neu"}, headers=AUTH).json()
    assert r["confirmed"] is False and len(r["preview"]["drafts_released"]) == 2
    with SessionLocal() as db:
        assert db.get(Campaign, cid).auto_approve is False
    r = client.patch(url, json={"auto_approve": True, "name": "Neu", "confirm": True}, headers=AUTH).json()
    assert r["confirmed"] is True and r["drafts_released"] == 2 and r["campaign"]["name"] == "Neu"
    assert set(_statuses(ids).values()) == {("scheduled", "approved")}


def test_reject_draft(client, db_data):
    cid = db_data["campaign_alpha"]
    a, _ = _with_drafts(client, cid)
    draft = next(d for d in client.get(f"/api/v1/campaigns/{cid}/drafts", headers=AUTH).json()["drafts"] if d["lead"]["id"] == a)
    url = f"/api/v1/drafts/{draft['draft_id']}/reject"
    assert client.post(url, json={}, headers=AUTH).json()["confirmed"] is False
    assert _statuses([a])[a] == ("needs_review", "draft")
    assert client.post(url, json={"confirm": True}, headers=AUTH).json()["lead_status"] == "skipped"
    assert _statuses([a])[a] == ("skipped", "failed")
    assert client.post(url, json={"confirm": True}, headers=AUTH).status_code == 409


def test_approval_foreign_tenant_404(client, db_data):
    cb = db_data["campaign_beta"]
    with SessionLocal() as db:
        lead = Lead(tenant_id=db_data["beta"], campaign_id=cb, company="X", email="x@beta-kunde.ch", status="needs_review")
        db.add(lead)
        db.flush()
        msg = Message(lead_id=lead.id, tenant_id=db_data["beta"], direction="out", step=0, subject="s", body="b", status="draft")
        db.add(msg)
        db.commit()
        lid, mid = lead.id, msg.id
    assert client.get(f"/api/v1/campaigns/{cb}/drafts", headers=AUTH).status_code == 404
    assert client.post(f"/api/v1/campaigns/{cb}/drafts/approve", json={"all": True, "confirm": True}, headers=AUTH).status_code == 404
    assert client.patch(f"/api/v1/campaigns/{cb}", json={"auto_approve": True, "confirm": True}, headers=AUTH).status_code == 404
    assert client.post(f"/api/v1/drafts/{mid}/reject", json={"confirm": True}, headers=AUTH).status_code == 404
    # a foreign lead id inside an own campaign is refused as well
    r = client.post(f"/api/v1/campaigns/{db_data['campaign_alpha']}/drafts/approve", json={"lead_ids": [lid], "confirm": True}, headers=AUTH)
    assert r.status_code == 404
    with SessionLocal() as db:
        assert db.get(Message, mid).status == "draft"
        assert db.get(Lead, lid).status == "needs_review"
        assert db.get(Campaign, cb).auto_approve is False
