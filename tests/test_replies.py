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
