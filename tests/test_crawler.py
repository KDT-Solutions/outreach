from bs4 import BeautifulSoup

from app.services import crawler


def _emails(html: str) -> set[str]:
    return crawler._extract_emails(html, BeautifulSoup(html, "lxml"))


def test_url_encoded_mailto_is_decoded():
    html = '<a href="mailto:%20info.liestal@abtautomobile.ch">Liestal</a> <a href="mailto:info.reinach@abtautomobile.ch?subject=x">R</a>'
    assert _emails(html) == {"info.liestal@abtautomobile.ch", "info.reinach@abtautomobile.ch"}


def test_location_mailboxes_are_generic():
    assert crawler.is_generic("info.reinach@abtautomobile.ch")
    assert crawler.is_generic("kontakt-basel@firma.ch")
    assert crawler.is_generic("info@firma.ch")
    assert not crawler.is_generic("a.arifi@abtautomobile.ch")


def test_location_mailbox_matching_the_lead_address_ranks_first():
    emails = {"info.liestal@abt.ch", "info.reinach@abt.ch", "info.muttenz@abt.ch", "a.arifi@abt.ch", "b.keller@abt.ch"}
    assert crawler.rank_emails(emails, "abt.ch", "Hauptstrasse 1, 4153 Reinach BL")[0] == "info.reinach@abt.ch"
    # without a hint plain info@ still beats location mailboxes
    assert crawler.rank_emails(emails | {"info@abt.ch"}, "abt.ch")[0] == "info@abt.ch"


def test_contact_match_needs_first_name_or_initial():
    cands = ["info@garageschmid.ch", "doris.schmid@garageschmid.ch", "bettina.schmid@garageschmid.ch", "mathias.bittel@garageschmid.ch"]
    assert crawler.pick_for_contact(cands, "Bettina Schmid") == "bettina.schmid@garageschmid.ch"
    assert crawler.pick_for_contact(["info@g.ch", "b.schmid@g.ch", "doris.schmid@g.ch"], "Bettina Schmid") == "b.schmid@g.ch"
    # another person with the same last name is never picked
    assert crawler.pick_for_contact(["info@g.ch", "doris.schmid@g.ch"], "Bettina Schmid") is None
    assert crawler.pick_for_contact(["info@g.ch", "mueller@g.ch"], "Hans Müller") == "mueller@g.ch"
    assert crawler.pick_for_contact(["info@g.ch"], "Bettina Schmid") is None


def test_management_pages_come_right_after_impressum():
    html = "".join(f'<a href="/{p}">{p}</a>' for p in ("honda/kontakt-neuwagen", "toyota/kontakt-neuwagen", "firma/kontakt", "firma/geschaeftsleitung", "impressum"))
    links = crawler._contact_links(BeautifulSoup(html, "lxml"), "https://www.garage.ch/")
    assert links[:2] == ["https://www.garage.ch/impressum", "https://www.garage.ch/firma/geschaeftsleitung"]
