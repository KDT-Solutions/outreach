import json

from app.db import SessionLocal
from app.models import ApiAudit, Lead
from tests.conftest import AUTH, TOKEN

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}


def rpc(client, method, params=None, auth=AUTH):
    r = client.post("/mcp", headers={**HEADERS, **auth}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return r


def call(client, name, **arguments):
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200
    result = r.json()["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return True, json.loads(text[text.index("{"):])
    return False, json.loads(text)


def test_mcp_requires_token(client):
    assert rpc(client, "tools/list", auth={}).status_code == 401
    assert rpc(client, "tools/list", auth={"Authorization": f"Bearer {TOKEN}0"}).status_code == 401


def test_mcp_rejects_foreign_host(client):
    r = client.post("/mcp", headers={**HEADERS, **AUTH, "Host": "evil.example"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code in (400, 403, 421)


def test_mcp_lists_all_tools(client):
    tools = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools == {
        "list_tenants", "list_campaigns", "get_campaign", "create_campaign", "import_leads", "list_leads", "update_lead",
        "delete_lead", "unsuppress_lead", "set_sequence", "preview_email", "send_test", "start_campaign", "pause_campaign", "stop_campaign", "get_stats",
        "list_tenant_signatures", "set_tenant_signature", "update_campaign", "list_drafts", "approve_drafts", "reject_draft",
        "search_leads", "enrich_leads", "list_audit_log", "get_delivery_status",
        "wait_campaign", "wait_leads", "release_leads",
    }


def test_mcp_tenant_scope(client, db_data):
    err, body = call(client, "list_tenants")
    assert not err and [t["slug"] for t in body["tenants"]] == ["alpha"]
    err, body = call(client, "get_campaign", campaign_id=db_data["campaign_beta"])
    assert err and body["error"]["code"] == "not_found"


def test_mcp_import_confirm_and_duplicates(client, db_data):
    cid = db_data["campaign_alpha"]
    leads = [{"firma": "A AG", "email": "a@a.ch"}, {"firma": "A AG 2", "email": "a@a.ch"}]
    err, body = call(client, "import_leads", campaign_id=cid, leads=leads)
    assert not err and body["confirmed"] is False
    with SessionLocal() as db:
        assert db.query(Lead).count() == 0
    err, body = call(client, "import_leads", campaign_id=cid, leads=leads, confirm=True)
    assert len(body["created"]) == 1 and body["skipped"][0]["reason"] == "duplicate_in_request"
    with SessionLocal() as db:
        audit = db.query(ApiAudit).filter_by(tool="import_leads").order_by(ApiAudit.id).all()
        assert [(a.outcome, a.transport) for a in audit] == [("preview", "mcp"), ("applied", "mcp")]


def test_mcp_validation_error_is_structured(client, db_data):
    err, body = call(client, "set_sequence", campaign_id=db_data["campaign_alpha"], steps=[{"subject": "x", "body_text": "{{iban}}", "delay_days": 0}], confirm=True)
    assert err and body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["loc"] == ["steps", 0, "body_text"]
    assert "iban" in body["error"]["details"][0]["msg"]


def test_mcp_start_needs_confirm_send(client, db_data):
    cid = db_data["campaign_alpha"]
    steps = [{"subject": "Hallo {{firma}}", "body_text": "Text", "delay_days": 0}]
    call(client, "set_sequence", campaign_id=cid, steps=steps, confirm=True)
    err, body = call(client, "start_campaign", campaign_id=cid, confirm=True)
    assert err and body["error"]["code"] == "confirm_send_required"
    err, body = call(client, "start_campaign", campaign_id=cid, confirm=True, confirm_send=True)
    assert not err and body["campaign"]["status"] == "active"


def test_mcp_approve_drafts_preview_and_foreign(client, db_data):
    from app.models import Message

    cid = db_data["campaign_alpha"]
    err, body = call(client, "import_leads", campaign_id=cid, leads=[{"firma": "A AG", "email": "a@a.ch"}], confirm=True)
    lead_id = body["created"][0]["lead_id"]
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        lead.status = "needs_review"
        db.add(Message(lead_id=lead_id, tenant_id=lead.tenant_id, direction="out", step=0, subject="s", body="b", status="draft"))
        db.commit()
    err, body = call(client, "approve_drafts", campaign_id=cid, all=True)
    assert not err and body["confirmed"] is False and len(body["preview"]["would_approve"]) == 1
    err, body = call(client, "approve_drafts", campaign_id=cid, all=True, confirm=True)
    assert not err and len(body["approved"]) == 1
    err, body = call(client, "list_drafts", campaign_id=db_data["campaign_beta"])
    assert err and body["error"]["code"] == "not_found"
