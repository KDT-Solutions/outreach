"""Reply handling: classification parsing and readable reply text."""

import pytest

from app.services import llm, mailtext


@pytest.mark.parametrize("answer, expected", [
    ("not_interested", "not_interested"),
    ("Not_Interested.", "not_interested"),
    ("not-interested", "not_interested"),
    ("interested", "interested"),
    ("out_of_office", "out_of_office"),
    ("unsubscribe", "unsubscribe"),
    ("keine Ahnung", "other"),
])
def test_parse_reply_class(answer, expected):
    assert llm.parse_reply_class(answer) == expected


HTML = (
    "<div>Guten Tag Herr Thurnherr</div><div>Leider sehe ich keinen Bedarf für diese Produkte.</div>"
    "<div>&nbsp;</div><div>Freundliche Grüsse</div><div>Roland Kiefer<br>Geschäftsführer</div>"
    "<div>Neu im Sortiment</div>"
    "<div><b>Von:</b> Kim Thurnherr &lt;info@dodolock.ch&gt;<br><b>Gesendet:</b> Dienstag, 20. Oktober 2026 09:41</div>"
    "<div>Guten Tag Herr Kiefer</div><div>Ursprünglicher Text.</div>"
)


def test_html_keeps_line_breaks_and_splits_quote():
    text = mailtext.html_to_text(HTML)
    reply, quoted = mailtext.split_quote(text)
    assert reply.splitlines()[0] == "Guten Tag Herr Thurnherr"
    assert "Leider sehe ich keinen Bedarf" in reply
    assert "Roland Kiefer\nGeschäftsführer" in reply
    assert "Von:" not in reply
    assert quoted.startswith("Von: Kim Thurnherr")
    assert "Ursprünglicher Text." in quoted


@pytest.mark.parametrize("line", [
    "-----Ursprüngliche Nachricht-----",
    "Am 20.10.2026 um 09:41 schrieb Kim Thurnherr <info@dodolock.ch>:",
    "Le mar. 20 oct. 2026 à 09:41, Kim Thurnherr a écrit :",
    "> zitierte Zeile",
])
def test_quote_markers(line):
    reply, quoted = mailtext.split_quote(f"Danke, kein Interesse.\n\n{line}\nalt")
    assert reply == "Danke, kein Interesse."
    assert quoted.startswith(line.strip()[:5])


def test_plain_reply_without_quote():
    assert mailtext.split_quote("Gerne mehr Infos.") == ("Gerne mehr Infos.", "")


def test_repair_replies_once(monkeypatch, db_data):
    from app import crypto, pipeline
    from app.db import SessionLocal
    from app.models import Event, Lead, Message, Tenant
    from app.services import zammad
    from tests.test_replies import HTML

    notes = []
    monkeypatch.setattr(zammad, "first_article", lambda url, tok, tid: {"body": HTML, "content_type": "text/html"})
    monkeypatch.setattr(zammad, "add_note", lambda url, tok, tid, body: notes.append((tid, body)) or True)
    seen = []
    monkeypatch.setattr(llm, "classify_reply", lambda subject, body: seen.append(body) or "not_interested")
    with SessionLocal() as db:
        t = db.get(Tenant, db_data["alpha"])
        t.zammad_url = "https://zammad.example"
        t.zammad_token_enc = crypto.encrypt("tok")
        lead = Lead(tenant_id=t.id, campaign_id=db_data["campaign_alpha"], company="Regio", email="r@regio.example", status="replied")
        db.add(lead)
        db.flush()
        db.add(Message(lead_id=lead.id, tenant_id=t.id, direction="in", step=1, subject="AW", body="alles in einer Zeile",
                       status="received", classification="interested", zammad_ticket_id=42))
        db.commit()
        assert pipeline.repair_replies_once(db) == 1
        m = db.query(Message).filter_by(zammad_ticket_id=42).one()
        assert m.classification == "not_interested"
        assert "Herr Thurnherr\n" in m.body
        assert "Von:" not in seen[0]  # classified without the quote
        assert notes == [(42, notes[0][1])] and "interested auf not_interested" in notes[0][1]
        # second run does nothing
        assert pipeline.repair_replies_once(db) == 0
        assert len(notes) == 1
        assert db.query(Event).filter_by(kind="migration").count() == 1
