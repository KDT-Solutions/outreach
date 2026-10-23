from bs4 import BeautifulSoup

from app.services import crawler


def _emails(html: str) -> set[str]:
    return set(crawler._extract_emails(html, BeautifulSoup(html, "lxml")))


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


def test_people_pages_first_then_contact_then_impressum():
    paths = ("honda/kontakt-neuwagen", "firma/kontakt", "impressum", "firma/mitarbeiter/werkstatt", "firma/geschaeftsleitung",
             "firma/mitarbeiter/verkauf", "ueber-uns")
    html = "".join(f'<a href="/{p}">{p}</a>' for p in paths)
    links = crawler._contact_links(BeautifulSoup(html, "lxml"), "https://www.garage.example/")
    base = "https://www.garage.example/"
    assert links[:4] == [base + "firma/geschaeftsleitung", base + "ueber-uns", base + "firma/kontakt", base + "impressum"]
    assert set(links[4:]) == {base + "honda/kontakt-neuwagen", base + "firma/mitarbeiter/werkstatt", base + "firma/mitarbeiter/verkauf"}
    # only two subpages (API mode): one people page plus the contact page
    assert crawler._contact_links(BeautifulSoup(html, "lxml"), base, budget=2)[:2] == [base + "firma/geschaeftsleitung", base + "firma/kontakt"]


def test_team_excerpt_comes_from_people_and_contact_pages():
    from unittest import mock

    pages = {
        "https://m.example/": '<a href="/impressum">Impressum</a><a href="/team">Team</a><a href="/kontakt">Kontakt</a>',
        "https://m.example/team": "<p>Anna Muster, Geschäftsführerin</p>",
        "https://m.example/kontakt": "<p>Werkstatt: Beat Beispiel</p>",
        "https://m.example/impressum": "<p>Verwaltungsrat: Carl Rat</p>",
    }
    with mock.patch.object(crawler, "_fetch", side_effect=lambda c, url, m: (pages.get(url), url)):
        res = crawler.crawl("https://m.example/")
    assert "Anna Muster, Geschäftsführerin" in res.team_excerpt and "Beat Beispiel" in res.team_excerpt
    assert "Carl Rat" not in res.team_excerpt and "Carl Rat" in res.impressum_excerpt


# ---------------------------------------------------------------- SSRF guard
import pytest
import httpx
from unittest import mock


def _dns(table: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in table:
            raise crawler.UnsafeURL(f"{host}: nicht auflösbar")
        return table[host]
    return resolve


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:80/admin",
    "http://10.0.0.5/",
    "http://192.168.100.1:80/",
    "http://172.16.3.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://localhost/",
    "http://portainer.local/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "gopher://example.com/",
    "http://example.com:8080/",
    "https://example.com:9000/",
    "http://user:pw@example.com/",
])
def test_check_url_refuses_private_and_odd_targets(url):
    with mock.patch.object(crawler, "_resolve", _dns({"example.com": ["93.184.216.34"]})):
        with pytest.raises(crawler.UnsafeURL):
            crawler.check_url(url)


def test_check_url_refuses_host_resolving_to_private_address():
    table = {"intern.example": ["10.1.2.3"], "mixed.example": ["93.184.216.34", "192.168.1.1"], "v6.example": ["fe80::1"]}
    with mock.patch.object(crawler, "_resolve", _dns(table)):
        for host in table:
            with pytest.raises(crawler.UnsafeURL):
                crawler.check_url(f"https://{host}/")


def test_check_url_accepts_public_host_and_pins_ip():
    with mock.patch.object(crawler, "_resolve", _dns({"firma.example": ["93.184.216.34"], "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]})):
        assert crawler.check_url("https://firma.example/kontakt") == ("firma.example", 443, "93.184.216.34")
        assert crawler.check_url("http://firma.example:80/") == ("firma.example", 80, "93.184.216.34")
        host, port, ip = crawler.check_url("https://v6.example/")
        assert crawler._pinned("https://v6.example/impressum?x=1", host, port, ip) == "https://[2606:2800:220:1:248:1893:25c8:1946]/impressum?x=1"
    assert crawler._pinned("https://firma.example/a", "firma.example", 443, "93.184.216.34") == "https://93.184.216.34/a"
    assert crawler._pinned("http://firma.example/a", "firma.example", 443, "93.184.216.34") == "http://93.184.216.34:443/a"


def _crawl_with(handler, table):
    """crawl() against an httpx mock transport; requests arrive at the pinned IP with the Host header."""
    seen: list[tuple[str, str]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("host", ""), str(request.url)))
        return handler(request)

    def make_client(timeout, headers):
        return httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, transport=httpx.MockTransport(wrapped))

    with mock.patch.object(crawler, "_resolve", _dns(table)), mock.patch.object(crawler, "_make_client", make_client):
        res = crawler.crawl("https://firma.example/")
    return res, seen


def test_crawl_requests_go_to_the_checked_ip_with_host_header():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, html='<a href="/impressum">Impressum</a>')
        return httpx.Response(200, headers={"content-type": "text/html"}, html="<p>info@firma.example</p>")

    res, seen = _crawl_with(handler, {"firma.example": ["93.184.216.34"]})
    assert res.best_email == "info@firma.example" and res.pages_fetched == 2
    assert all(host == "firma.example" and url.startswith("https://93.184.216.34/") for host, url in seen), seen


def test_crawl_refuses_redirect_to_private_address():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})
        raise AssertionError(f"request to {request.url} must not happen")

    res, seen = _crawl_with(handler, {"firma.example": ["93.184.216.34"]})
    assert res.error == "Startseite nicht abrufbar" and len(seen) == 1 and not res.site_excerpt


def test_crawl_refuses_redirect_to_host_resolving_privately_and_limits_hops():
    def handler(request):
        if request.headers["host"] == "firma.example":
            return httpx.Response(301, headers={"location": "https://cdn.intern.example/"})
        raise AssertionError(f"request to {request.url} must not happen")

    res, seen = _crawl_with(handler, {"firma.example": ["93.184.216.34"], "cdn.intern.example": ["192.168.100.10"]})
    assert res.error == "Startseite nicht abrufbar" and len(seen) == 1

    def loop(request):
        return httpx.Response(302, headers={"location": "https://firma.example/next"})

    res, seen = _crawl_with(loop, {"firma.example": ["93.184.216.34"]})
    assert res.error == "Startseite nicht abrufbar" and len(seen) == crawler.MAX_REDIRECTS + 1


def test_crawl_follows_public_redirect_and_checks_every_hop():
    def handler(request):
        if request.headers["host"] == "firma.example":
            return httpx.Response(301, headers={"location": "https://www.firma.example/"})
        return httpx.Response(200, headers={"content-type": "text/html"}, html="<p>kontakt@firma.example</p>")

    res, seen = _crawl_with(handler, {"firma.example": ["93.184.216.34"], "www.firma.example": ["93.184.216.35"]})
    assert res.best_email == "kontakt@firma.example" and res.domain == "firma.example"
    assert seen[1] == ("www.firma.example", "https://93.184.216.35/")


def test_crawl_only_reads_html_and_caps_body_size():
    big = "<p>" + "Lorem ipsum dolor sit amet. " * 110_000 + " info@firma.example</p>"  # about 3 MB

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "application/json"}, text='{"x": 1}')
        return httpx.Response(200, headers={"content-type": "text/html"}, text=big)

    res, _ = _crawl_with(handler, {"firma.example": ["93.184.216.34"]})
    assert res.error == "Startseite nicht abrufbar"

    # declared size above the limit: not read at all
    res, _ = _crawl_with(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=big), {"firma.example": ["93.184.216.34"]})
    assert res.error == "Startseite nicht abrufbar"

    # chunked without Content-Length: reading stops at the limit, the address beyond it is never seen
    def chunked(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=iter([big[i:i + 65536].encode() for i in range(0, len(big), 65536)]))

    res, _ = _crawl_with(chunked, {"firma.example": ["93.184.216.34"]})
    assert res.error is None and res.best_email is None and res.pages_fetched == 1
    assert len(res.site_excerpt) <= 3000


def test_safe_website_only_accepts_http_and_https():
    assert crawler.safe_website("https://firma.example/") == "https://firma.example/"
    assert crawler.safe_website(" HTTP://firma.example ") == "HTTP://firma.example"
    for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "//firma.example", "firma.example", "https://u:p@x.example/", "", None):
        assert crawler.safe_website(bad) is None
