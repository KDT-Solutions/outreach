from unittest import mock

import pytest

from app.apiv1 import prospect
from app.db import SessionLocal
from app.models import ApiAudit, Lead, Suppression
from app.pipeline import EmailFinding
from app.services import crawler, places
from tests.conftest import AUTH


def hit(pid, name, address="Musterweg 1, 4000 Basel, Schweiz", lat=47.56, lng=7.59):
    return places.PlaceHit(place_id=pid, name=name, address=address, types=["car_repair"], lat=lat, lng=lng)


# ---------------------------------------------------------------- Places hit -> lead
def test_place_hit_becomes_lead_candidate():
    p = prospect.from_place(3, hit("p1", "Muster Garage AG", "Musterweg 1, 4000 Basel, Schweiz"), "https://muster.example", "061 000 00 00")
    assert (p.index, p.firma, p.plz, p.ort, p.website, p.place_id) == (3, "Muster Garage AG", "4000", "Basel", "https://muster.example", "p1")
    assert prospect.lead_note(p) == "Tel. 061 000 00 00"
    assert places.city_of("Rue Exemple 2, 1000 Lausanne, Suisse") == ("1000", "Lausanne")
    assert places.city_of(None) == (None, None)


def test_name_match_for_website_lookup():
    assert prospect.names_match("Muster AG", "Muster AG Basel")
    assert prospect.names_match("Garage Müller GmbH", "Garage Mueller")
    assert not prospect.names_match("Muster AG", "Beispiel AG")
    assert not prospect.names_match("AG", "Muster AG")


# ---------------------------------------------------------------- address choice in the crawler
def test_generic_before_personal_and_foreign_domains_dropped():
    found = ["anna.muster@muster.example", "info@muster.example", "kontakt@muster.example", "web@agentur.example", "chef@gmail.com"]
    ranked = crawler.rank_emails(found, "muster.example", prefer_generic=True, own_domain_only=True)
    assert ranked == ["info@muster.example", "kontakt@muster.example", "anna.muster@muster.example"]
    # office@ / mail@ are generic as well, in this order
    assert crawler.rank_emails(["mail@m.example", "office@m.example", "a.b@m.example"], "m.example", prefer_generic=True)[:2] == ["office@m.example", "mail@m.example"]
    # no generic mailbox: first own-domain address in site order
    assert crawler.rank_emails(["zora@m.example", "anton@m.example"], "m.example", prefer_generic=True, own_domain_only=True) == ["zora@m.example", "anton@m.example"]
    # only foreign addresses: nothing
    assert crawler.rank_emails(["web@agentur.example"], "m.example", prefer_generic=True, own_domain_only=True) == []
    # web UI default unchanged: a single personal address still wins
    assert crawler.rank_emails({"anna@m.example", "info@m.example"}, "m.example")[0] == "anna@m.example"


def test_crawl_respects_page_limit():
    pages = {
        "https://m.example/": '<a href="/impressum">Impressum</a><a href="/kontakt">Kontakt</a><a href="/team">Team</a> info@m.example',
        "https://m.example/impressum": "anna@m.example",
        "https://m.example/kontakt": "kontakt@m.example",
        "https://m.example/team": "team@m.example",
    }
    fetched = []

    def fake_fetch(client, url, max_bytes):
        fetched.append(url)
        return pages.get(url), url

    with mock.patch.object(crawler, "_fetch", side_effect=fake_fetch):
        res = crawler.crawl("https://m.example/", prefer_generic=True, own_domain_only=True, max_pages=3, timeout=10)
    assert fetched == ["https://m.example/", "https://m.example/impressum", "https://m.example/kontakt"]
    assert res.best_email == "info@m.example"


# ---------------------------------------------------------------- search_leads / enrich_leads
FINDINGS = {
    "https://a.example": "info@a.example",
    "https://c.example": "info@c.example",      # already a lead at the tenant
    "https://d.example": "info@d.example",      # domain suppressed
    "https://e.example": None,                  # nothing found
    "https://f.example": "info@a.example",      # same address as A in this call
}


def fake_find_email(website, place_hint=None, **opts):
    assert opts == {"prefer_generic": True, "own_domain_only": True, "max_pages": 3, "timeout": 10.0}
    if website == "https://g.example":
        raise RuntimeError("TLS kaputt")
    email = FINDINGS.get(website)
    return EmailFinding(email, "crawler" if email else None, crawler.CrawlResult(domain=crawler.domain_of(website), emails=[email] if email else []))


@pytest.fixture
def search_env(db_data):
    hits = [hit(f"p{x}", f"Firma {x.upper()}") for x in "abcdefg"] + [hit("pfar", "Weit Weg AG", lat=46.0, lng=9.0)]
    websites = {f"p{x}": f"https://{x}.example" for x in "abcdefg"}
    with SessionLocal() as db:
        db.add(Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="B alt", place_id="pb", email="alt@b.example"))
        db.add(Lead(tenant_id=db_data["alpha"], campaign_id=db_data["campaign_alpha"], company="C alt", email="info@c.example"))
        db.add(Suppression(tenant_id=db_data["alpha"], value="@d.example", reason="manual"))
        db.commit()
    with mock.patch.object(places, "locate", return_value=(47.56, 7.59)), \
         mock.patch.object(places, "search_text", return_value=hits) as search, \
         mock.patch.object(places, "details", side_effect=lambda pid: (websites.get(pid), "061 000 00 00")) as details, \
         mock.patch("app.pipeline.find_email", side_effect=fake_find_email) as find, \
         mock.patch("app.services.verify.check", return_value=True):
        yield {"search": search, "details": details, "find": find}


def test_search_leads_preview_lists_hits_without_writing(client, db_data, search_env):
    url = f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads/search"
    r = client.post(url, json={"query": "Garage", "region": "Basel", "radius_km": 25}, headers=AUTH).json()
    assert r["confirmed"] is False
    hits = r["preview"]["hits"]
    assert [h["firma"] for h in hits] == [f"Firma {x.upper()}" for x in "abcdefg"]  # Weit Weg AG outside the radius
    assert hits[0]["website"] == "https://a.example" and hits[0]["ort"] == "Basel"
    assert hits[1]["status"] == "duplicate" and hits[1]["reason"] == "exists"
    search_env["find"].assert_not_called()  # preview: no crawling
    assert search_env["search"].call_args.kwargs["bias"] == (47.56, 7.59, 25000.0)
    with SessionLocal() as db:
        assert db.query(Lead).count() == 2


def test_search_leads_creates_and_reports_duplicates(client, db_data, search_env):
    url = f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads/search"
    r = client.post(url, json={"query": "Garage", "region": "Basel", "confirm": True}, headers=AUTH).json()
    by_firma = {row["firma"]: row for row in r["leads"]}
    assert by_firma["Firma A"]["status"] == "enriched" and by_firma["Firma A"]["email"] == "info@a.example"
    assert (by_firma["Firma B"]["status"], by_firma["Firma B"]["reason"]) == ("duplicate", "exists")
    assert (by_firma["Firma C"]["status"], by_firma["Firma C"]["reason"]) == ("duplicate", "exists")
    assert (by_firma["Firma D"]["status"], by_firma["Firma D"]["reason"]) == ("duplicate", "suppressed")
    assert by_firma["Firma E"]["status"] == "no_email" and by_firma["Firma E"]["note"] == "keine Adresse gefunden"
    assert (by_firma["Firma F"]["status"], by_firma["Firma F"]["reason"]) == ("duplicate", "duplicate_in_request")
    assert by_firma["Firma G"]["status"] == "error" and "TLS kaputt" in by_firma["Firma G"]["error"]
    assert (r["found"], r["created"], r["no_email"], r["duplicates"], r["errors"]) == (7, 1, 1, 4, 1)
    with SessionLocal() as db:
        a = db.get(Lead, by_firma["Firma A"]["lead_id"])
        assert (a.status, a.email_source, a.city, a.place_id, a.notes, a.language) == ("enriched", "crawler", "Basel", "pa", "Tel. 061 000 00 00", "de")
        e = db.get(Lead, by_firma["Firma E"]["lead_id"])
        assert e.status == "no_email" and "keine Adresse gefunden" in e.notes
        assert db.query(Lead).filter(Lead.place_id == "pg").count() == 0  # errors are not stored, can be retried
        audit = db.query(ApiAudit).filter_by(tool="search_leads").one()
        assert audit.outcome == "applied" and set(audit.object_ids["lead_ids"]) == {a.id, e.id}
    # same search again: everything is a duplicate now except the error
    r = client.post(url, json={"query": "Garage", "region": "Basel", "confirm": True}, headers=AUTH).json()
    assert r["created"] == 0 and r["no_email"] == 0


def test_enrich_leads_resolves_website_by_name(client, db_data):
    url = f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads/enrich"
    hits = {"Muster AG 4000 Basel": [hit("pm", "Muster AG")], "Beispiel GmbH Bern": [hit("px", "Ganz Andere AG")]}
    with mock.patch.object(places, "search_text", side_effect=lambda q, **kw: hits.get(q, [])), \
         mock.patch.object(places, "details", return_value=("https://muster.example", None)), \
         mock.patch("app.pipeline.find_email", side_effect=lambda w, place_hint=None, **o: EmailFinding(
             "info@muster.example" if "muster" in w else "info@own.example", "crawler",
             crawler.CrawlResult(domain=crawler.domain_of(w), emails=[]))), \
         mock.patch("app.services.verify.check", return_value=True):
        body = {"leads": [{"firma": "Muster AG", "ort": "Basel", "plz": "4000"}, {"firma": "Beispiel GmbH", "ort": "Bern"},
                          {"firma": "Eigene AG", "website": "https://own.example"}]}
        r = client.post(url, json=body, headers=AUTH).json()
        assert r["confirmed"] is False
        rows = r["preview"]["leads"]
        assert [(x["status"], x["email"]) for x in rows] == [("enriched", "info@muster.example"), ("no_email", None), ("enriched", "info@own.example")]
        assert rows[1]["note"] == "keine Adresse gefunden (keine Webseite gefunden)"
        with SessionLocal() as db:
            assert db.query(Lead).count() == 0
        r = client.post(url, json={**body, "confirm": True}, headers=AUTH).json()
    assert (r["created"], r["no_email"]) == (2, 1)
    with SessionLocal() as db:
        m = db.query(Lead).filter_by(email="info@muster.example").one()
        assert (m.website, m.place_id, m.email_source, m.status) == ("https://muster.example", "pm", "crawler", "enriched")
        b = db.query(Lead).filter_by(company="Beispiel GmbH").one()
        assert b.status == "no_email" and b.website is None


def test_prospect_limits_and_foreign_tenant(client, db_data):
    too_many = {"leads": [{"firma": f"F{i}"} for i in range(201)]}
    assert client.post(f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads/enrich", json=too_many, headers=AUTH).status_code == 422
    assert client.post(f"/api/v1/campaigns/{db_data['campaign_alpha']}/leads/search", json={"query": "x", "limit": 201}, headers=AUTH).status_code == 422
    with mock.patch.object(places, "search_text") as search:
        r = client.post(f"/api/v1/campaigns/{db_data['campaign_beta']}/leads/search", json={"query": "x", "confirm": True}, headers=AUTH)
        assert r.status_code == 404
        r = client.post(f"/api/v1/campaigns/{db_data['campaign_beta']}/leads/enrich", json={"leads": [{"firma": "X"}], "confirm": True}, headers=AUTH)
        assert r.status_code == 404
        search.assert_not_called()
