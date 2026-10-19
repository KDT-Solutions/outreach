"""Draft generation: tolerant JSON parsing, one corrective retry, and no endless retries."""

from types import SimpleNamespace

import pytest

from app import pipeline
from app.db import SessionLocal
from app.models import Campaign, Event, Lead
from app.services import llm
from tests.conftest import AUTH

GOOD = '{"contact_name": null, "salutation": "Guten Tag", "subject": "Kurze Frage", "body": "Guten Tag\\n\\nText."}'


def _msg(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop)


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return _msg(self.answers.pop(0))


@pytest.fixture
def ai_lead(db_data):
    with SessionLocal() as db:
        c = db.get(Campaign, db_data["campaign_alpha"])
        c.status = "active"
        c.steps = [{"delay_days": 0, "instruction": "Erstkontakt"}]
        lead = Lead(tenant_id=db_data["alpha"], campaign_id=c.id, company="Garage AG", email="info@garage-ag.example", status="enriched")
        db.add(lead)
        db.commit()
        return lead.id


def _fake(monkeypatch, answers):
    client = FakeClient(answers)
    monkeypatch.setattr(llm, "_client", lambda: client)
    return client


@pytest.mark.parametrize("text", [
    GOOD,
    "```json\n" + GOOD + "\n```",
    "Hier ist die Mail:\n" + GOOD + "\nViel Erfolg {sic}",
])
def test_json_from_tolerates_wrapping(text):
    assert llm._json_from(text)["subject"] == "Kurze Frage"


def test_json_from_rejects_plain_text():
    with pytest.raises(llm.NoJsonError):
        llm._json_from("Mir fehlen Angaben zum Empfänger.")


def test_retry_once_then_succeed(monkeypatch, ai_lead):
    client = _fake(monkeypatch, ["Mir fehlen Angaben zum Empfänger.", GOOD])
    with SessionLocal() as db:
        assert pipeline.generate_batch(db, 10) == 1
        assert db.get(Lead, ai_lead).status == "needs_review"
    assert len(client.calls) == 2
    # the retry carries the first answer and a corrective instruction
    assert client.calls[1]["messages"][-2]["role"] == "assistant"


def test_error_shows_model_answer(monkeypatch, ai_lead):
    _fake(monkeypatch, ["Mir fehlen Angaben.", "Leider nicht möglich."])
    with SessionLocal() as db:
        pipeline.generate_batch(db, 10)
        lead = db.get(Lead, ai_lead)
        assert "Leider nicht möglich." in lead.last_error
        assert "stop_reason=end_turn" in lead.last_error
        assert lead.status == "enriched"


def test_parks_lead_after_three_failures_and_update_resets(monkeypatch, client, ai_lead):
    _fake(monkeypatch, ["nein"] * 6)
    with SessionLocal() as db:
        for _ in range(3):
            pipeline.generate_batch(db, 10)
        lead = db.get(Lead, ai_lead)
        assert lead.status == "draft_failed"
        assert db.query(Event).filter_by(lead_id=ai_lead, kind="generate_gave_up").count() == 1

    # parked: further worker cycles do not call the model
    spy = _fake(monkeypatch, [])
    with SessionLocal() as db:
        assert pipeline.generate_batch(db, 10) == 0
    assert spy.calls == []

    # a lead change brings it back with a fresh count
    r = client.patch(f"/api/v1/leads/{ai_lead}", json={"nachname": "Muster", "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        lead = db.get(Lead, ai_lead)
        assert lead.status == "enriched"
        assert pipeline.generate_failures(db, ai_lead) == 0


def test_set_salutation_is_kept_and_passed_to_model(monkeypatch, ai_lead):
    with SessionLocal() as db:
        lead = db.get(Lead, ai_lead)
        lead.salutation = "Guten Tag Frau Muster"
        db.commit()
    other = GOOD.replace('"salutation": "Guten Tag"', '"salutation": "Guten Tag Herr Muster"')
    client = _fake(monkeypatch, [other])
    with SessionLocal() as db:
        pipeline.generate_batch(db, 10)
        assert db.get(Lead, ai_lead).salutation == "Guten Tag Frau Muster"
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Vorgegebene Anrede: Guten Tag Frau Muster" in prompt
    assert client.calls[0]["max_tokens"] == llm.MAX_TOKENS
