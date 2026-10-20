from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app as ui_app
from app.models import Lead, Suppression

from .conftest import AUTH


def _lead(db_data, language=None, status="in_sequence"):
    with SessionLocal() as db:
        lead = Lead(
            tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="Kunde AG",
            email="kunde@example.com", status=status, language=language,
        )
        db.add(lead)
        db.commit()
        return lead.id, lead.unsubscribe_token


def _get(lead_id):
    with SessionLocal() as db:
        return db.get(Lead, lead_id)


def test_get_does_not_unsubscribe(db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        r = c.get(f"/u/{token}")
    assert r.status_code == 200
    assert 'action="/u/' + token + '/abmelden"' in r.text and "Abmelden" in r.text
    assert _get(lead_id).status == "in_sequence"
    with SessionLocal() as db:
        assert db.query(Suppression).count() == 0


def test_confirm_with_reason(db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        r = c.post(f"/u/{token}/abmelden", data={"reason": "bestehender_lieferant", "comment": "  Wir führen bereits ein System.  "})
        assert r.status_code == 200 and "Abgemeldet" in r.text
        # page shown again afterwards: feedback form, no second unsubscribe button
        again = c.get(f"/u/{token}")
        assert "/grund" in again.text
    lead = _get(lead_id)
    assert lead.status == "unsubscribed" and lead.unsubscribed_at is not None
    assert lead.unsubscribe_reason == "bestehender_lieferant"
    assert lead.unsubscribe_comment == "Wir führen bereits ein System."
    with SessionLocal() as db:
        assert db.query(Suppression).filter(Suppression.value == "kunde@example.com").count() == 1


def test_confirm_without_reason_and_unknown_reason(db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        c.post(f"/u/{token}/abmelden", data={"reason": "<script>"})
    lead = _get(lead_id)
    assert lead.status == "unsubscribed" and lead.unsubscribe_reason is None


def test_one_click_then_reason(db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        c.post(f"/u/{token}", content="List-Unsubscribe=One-Click", headers={"content-type": "application/x-www-form-urlencoded"})
        assert _get(lead_id).status == "unsubscribed"
        r = c.post(f"/u/{token}/grund", data={"reason": "konditionen"})
    assert "Vielen Dank" in r.text
    assert _get(lead_id).unsubscribe_reason == "konditionen"


def test_french_page(db_data):
    _, token = _lead(db_data, language="fr")
    with TestClient(ui_app, base_url="https://testserver") as c:
        r = c.get(f"/u/{token}")
    assert 'lang="fr"' in r.text and "Se désinscrire" in r.text


def test_unknown_token_404(db_data):
    with TestClient(ui_app, base_url="https://testserver") as c:
        assert c.get("/u/doesnotexist").status_code == 404


def test_stats_and_lead_show_reason(client, db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        c.post(f"/u/{token}/abmelden", data={"reason": "falsche_person"})
    stats = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/stats", headers=AUTH)
    assert stats.status_code == 200, stats.text
    assert stats.json()["unsubscribe_reasons"] == {"falsche_person": 1}
    leads = client.get(f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads", headers=AUTH).json()
    row = next(l for l in leads["leads"] if l["id"] == lead_id)
    assert row["unsubscribe_reason"] == "falsche_person"


def test_source_recorded(db_data):
    lead_id, token = _lead(db_data)
    with TestClient(ui_app, base_url="https://testserver") as c:
        c.post(f"/u/{token}/abmelden", data={})
    assert _get(lead_id).unsubscribe_source == "page"


def test_backfill_old_unsubscribe(db_data):
    from app import db as dbmod
    from app.pipeline import suppress

    lead_id, _ = _lead(db_data)
    with SessionLocal() as db:
        suppress(db, db_data["alpha"], "kunde@example.com", "unsubscribe")
        lead = db.get(Lead, lead_id)
        lead.unsubscribed_at = None  # state before the column existed
        db.commit()
    dbmod._backfill_unsubscribes()
    lead = _get(lead_id)
    assert lead.unsubscribed_at is not None and lead.unsubscribe_source == "link"
