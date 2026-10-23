"""Fetch a company website, find Impressum/Kontakt pages and extract e-mail addresses.

Swiss companies have to publish a contact address in the Impressum, so this
finds a usable mailbox for most SMEs without any paid data source.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from ..config import get_settings

log = logging.getLogger(__name__)

# Pages that show the people (owner, management, team), most telling first. They name the
# best contact person; the Impressum often only lists board members or legal representatives.
PEOPLE_HINTS = (
    "geschaeftsleitung", "geschäftsleitung", "geschaftsleitung", "inhaber", "management", "direction", "team", "equipe",
    "équipe", "ueber-uns", "über-uns", "uber-uns", "about", "a-propos", "qui-sommes", "portrait", "unternehmen", "mitarbeit",
)
CONTACT_HINTS = ("impressum", "kontakt", "contact", "imprint", "mentions-legales", "legal") + PEOPLE_HINTS
# people pages fetched before the contact page; more only if pages are left over
PEOPLE_PAGES_FIRST = 2
# a long staff list must not push the contact page out of the excerpt
TEAM_EXCERPT_PER_PAGE = 2000
TEAM_EXCERPT_MAX = 6000
# no "%": URL-encoded mailto links ("%20info@...") must not leak into the address
EMAIL_RE = re.compile(r"[A-Z0-9._+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
STRICT_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$")
OBFUSCATED_RE = re.compile(
    r"([A-Z0-9._%+\-]+)\s*(?:\(at\)|\[at\]|\{at\}|\s@\s|&#64;|\(ät\)|\[ät\])\s*([A-Z0-9.\-]+)\s*(?:\(dot\)|\[dot\]|\.)\s*([A-Z]{2,})",
    re.I,
)
SKIP_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
# SSRF guard: the crawler only ever talks to public web servers on the standard ports.
# Lead websites come from Google Places and the API, i.e. from third parties.
ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 3
HTML_TYPES = ("text/html", "application/xhtml+xml")


class UnsafeURL(ValueError):
    """URL points somewhere the crawler must not go (private network, odd scheme or port)."""
SKIP_EMAIL_PARTS = ("example.", "sentry", "wixpress", "wordpress", "domain.", "email.com", "noreply", "no-reply", "@2x", "u003e")
GENERIC_LOCALPARTS = ("info", "kontakt", "contact", "office", "mail", "hello", "hallo", "welcome", "admin", "sekretariat", "empfang", "praxis", "shop", "verkauf", "sales", "support")


@dataclass
class CrawlResult:
    domain: str | None = None
    emails: list[str] = field(default_factory=list)
    best_email: str | None = None
    site_excerpt: str = ""
    impressum_excerpt: str = ""
    # text of the team / about / contact pages: where the contact person is chosen from
    team_excerpt: str = ""
    pages_fetched: int = 0
    error: str | None = None


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def safe_website(url: str | None) -> str | None:
    """A lead website that may be stored and rendered as a link: http(s) with a host name,
    nothing else (no javascript:, data:, file: ...). None when it does not qualify."""
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if not parts.hostname or parts.username or parts.password:
        return None
    return url[:300]


# ---------------------------------------------------------------- SSRF guard
def _resolve(host: str) -> list[str]:
    """All addresses a host name resolves to (IPv4 and IPv6). Tests patch this."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL(f"{host}: nicht auflösbar ({exc.strerror or exc})") from None
    return sorted({info[4][0] for info in infos})


def _is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_url(url: str) -> tuple[str, int, str]:
    """Validate scheme, port and host of a URL and resolve the host.

    Returns (host, port, ip): the address the request must be pinned to. Raises UnsafeURL for
    anything but http/https on 80/443 to a public address: loopback, RFC1918, link-local
    (169.254.0.0/16, cloud metadata), multicast, reserved, IPv6 ULA etc. are refused, and a
    host name whose addresses are not all public is refused too (no DNS-based pivoting)."""
    try:
        parts = urlparse(url)
    except ValueError as exc:
        raise UnsafeURL(f"ungültige URL: {exc}") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"Schema {scheme or '-'} nicht erlaubt")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host or parts.username or parts.password:
        raise UnsafeURL("Host fehlt oder URL enthält Zugangsdaten")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("ungültiger Port") from None
    if port not in ALLOWED_PORTS:
        raise UnsafeURL(f"Port {port} nicht erlaubt")
    try:
        ipaddress.ip_address(host.strip("[]"))
        addrs = [host.strip("[]")]
    except ValueError:
        if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
            raise UnsafeURL(f"{host}: interner Hostname")
        addrs = _resolve(host)
    if not addrs:
        raise UnsafeURL(f"{host}: keine Adresse")
    for addr in addrs:
        if not _is_public(addr):
            raise UnsafeURL(f"{host} zeigt auf eine nicht öffentliche Adresse ({addr})")
    return host, port, addrs[0]


def _pinned(url: str, host: str, port: int, ip: str) -> str:
    """The same URL addressed to the checked IP, so the connection goes where the check went
    (no DNS re-resolution between check and connect)."""
    parts = urlparse(url)
    netloc = f"[{ip}]" if ":" in ip else ip
    if port != (443 if parts.scheme == "https" else 80):
        netloc += f":{port}"
    return urlunparse(parts._replace(netloc=netloc))


def _text_of(soup: BeautifulSoup, limit: int) -> str:
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _extract_emails(html: str, soup: BeautifulSoup) -> dict[str, None]:
    """Addresses in the order they appear (mailto links first); dict = ordered set."""
    found: dict[str, None] = {}
    for a in soup.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        # mailto links are URL-encoded: "mailto:%20info@firma.ch" has a leading space
        addr = unquote(href[7:].split("?")[0]).strip()
        if addr:
            found[addr.lower()] = None
    # decode first: raw "%20info@firma.ch" in an href would otherwise yield "20info@firma.ch"
    for m in EMAIL_RE.findall(unquote(html)):
        found[m.lower()] = None
    for m in OBFUSCATED_RE.findall(html):
        found[f"{m[0]}@{m[1]}.{m[2]}".lower()] = None
    clean: dict[str, None] = {}
    for e in found:
        if e.endswith(SKIP_EMAIL_SUFFIX) or any(p in e for p in SKIP_EMAIL_PARTS):
            continue
        e = e.strip(" .,;:")
        if len(e) > 100 or e.count("@") != 1 or not STRICT_EMAIL_RE.match(e):
            continue
        clean[e] = None
    return clean


def _generic_parts(email: str) -> tuple[str, str] | None:
    """("info", "") for info@, ("info", "reinach") for info.reinach@ / info-reinach@, None if personal."""
    local = email.partition("@")[0].split("+")[0].lower()
    m = re.match(r"^([a-z]+)[.\-_](.+)$", local)
    head, rest = (m.group(1), m.group(2)) if m else (local, "")
    if local in GENERIC_LOCALPARTS:
        return local, ""
    if head in GENERIC_LOCALPARTS and rest:
        return head, rest
    return None


def is_generic(email: str) -> bool:
    return _generic_parts(email) is not None


def _location_match(email: str, place_hint: str | None) -> bool:
    """info.reinach@ matches a lead whose address mentions Reinach."""
    parts = _generic_parts(email)
    if not parts or not parts[1] or not place_hint:
        return False
    words = {w for w in re.split(r"[^a-zäöüéèàç]+", place_hint.lower()) if len(w) > 2}
    return parts[1] in words


# order of generic mailboxes when generic addresses are preferred (API search/enrich)
GENERIC_PREFERENCE = ("info", "kontakt", "contact", "office", "mail")


def rank_emails(
    emails, domain: str | None, place_hint: str | None = None, *, prefer_generic: bool = False, own_domain_only: bool = False
) -> list[str]:
    """Own-domain first. A single personal address on the company domain beats info@;
    with several personal addresses we cannot tell who is who, so info@ comes first.
    Among generic addresses: one for the lead's location (info.reinach@) first, then
    plain info@, then other generic ones.

    prefer_generic: generic mailboxes always first (info@, kontakt@, office@, mail@, then
    other generic ones), personal addresses after them in the order found on the site.
    own_domain_only: drop addresses on other domains (agency, web designer, gmail)."""
    def same(e: str) -> bool:
        dom = e.partition("@")[2]
        return bool(domain and (dom == domain or dom.endswith("." + domain)))

    emails = list(emails)
    own = [e for e in emails if same(e)]
    other = [] if own_domain_only else [e for e in emails if not same(e)]
    if prefer_generic:
        def generic_rank(e: str) -> tuple:
            head = (_generic_parts(e) or ("", ""))[0]
            pref = GENERIC_PREFERENCE.index(head) if head in GENERIC_PREFERENCE else len(GENERIC_PREFERENCE)
            return (not _location_match(e, place_hint), pref, (_generic_parts(e) or ("", ""))[1] != "")

        generic = sorted((e for e in own if is_generic(e)), key=generic_rank)  # stable: keeps site order
        return generic + [e for e in own if not is_generic(e)] + other

    own_personal = sorted(e for e in own if not is_generic(e))
    own_generic = sorted(
        (e for e in own if is_generic(e)),
        key=lambda e: (not _location_match(e, place_hint), e.split("@")[0] != "info", (_generic_parts(e) or ("", ""))[1] != "", e),
    )
    if len(own_personal) == 1:
        ordered = own_personal + own_generic
    else:
        ordered = own_generic + own_personal
    return ordered + sorted(other, key=lambda e: (not is_generic(e), e))


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "é": "e", "è": "e", "à": "a", "ç": "c"})


def pick_for_contact(candidates: list[str], contact_name: str | None) -> str | None:
    """The candidate that belongs to the named contact, or None if it is not clear.

    Bettina Schmid: bettina.schmid@ / bschmid@ / b.schmid@ (first name or initial plus
    last name) win; a bare last name only counts if exactly one address has it, so
    doris.schmid@ is never picked for Bettina Schmid."""
    if not contact_name or not candidates:
        return None
    names = [p.lower().translate(_UMLAUTS) for p in re.split(r"[\s\-]+", contact_name.strip()) if p.strip(".")]
    if len(names) < 2:
        return None
    first, last = names[0].strip("."), names[-1]
    if len(last) < 3:
        return None

    def tokens(e: str) -> list[str]:
        return [t for t in re.split(r"[.\-_]", e.partition("@")[0].lower().translate(_UMLAUTS)) if t]

    full, initial, last_only = [], [], []
    for e in candidates:
        if is_generic(e):
            continue
        t = tokens(e)
        joined = "".join(t)
        if last in t or joined.endswith(last):
            if len(first) > 1 and (first in t or joined == first + last):
                full.append(e)
            elif (len(t) >= 2 and t[0] == first[0]) or joined == first[0] + last:
                initial.append(e)
            elif joined == last:
                # bare "schmid@"; "doris.schmid@" belongs to someone else
                last_only.append(e)
    if full:
        return full[0]
    if len(initial) == 1:
        return initial[0]
    if not initial and len(last_only) == 1:
        return last_only[0]
    return None


def crawl(
    website: str,
    place_hint: str | None = None,
    *,
    prefer_generic: bool = False,
    own_domain_only: bool = False,
    max_pages: int | None = None,
    timeout: float | None = None,
) -> CrawlResult:
    """place_hint: the lead's postal address, to prefer info.<ort>@ for multi-site companies.
    max_pages counts the start page (default 5), timeout is per page (default CRAWL_TIMEOUT).
    prefer_generic / own_domain_only: see rank_emails."""
    s = get_settings()
    subpages = (max_pages - 1) if max_pages else 4
    res = CrawlResult(domain=domain_of(website))
    if not website:
        res.error = "keine Webseite"
        return res
    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    headers = {"User-Agent": s.crawl_user_agent, "Accept-Language": "de-CH,de;q=0.9,en;q=0.5"}
    emails: dict[str, None] = {}
    try:
        with _make_client(timeout or s.crawl_timeout, headers) as client:
            html, base = _fetch(client, website, s.crawl_max_bytes)
            res.pages_fetched += 1
            if html is None:
                res.error = "Startseite nicht abrufbar"
                return res
            res.domain = domain_of(base) or res.domain
            soup = BeautifulSoup(html, "lxml")
            emails |= _extract_emails(html, soup)
            links = _contact_links(soup, base, budget=max(0, subpages))
            res.site_excerpt = _text_of(BeautifulSoup(html, "lxml"), 3000)

            for url in links[:max(0, subpages)]:
                sub, _ = _fetch(client, url, s.crawl_max_bytes)
                if sub is None:
                    continue
                res.pages_fetched += 1
                sub_soup = BeautifulSoup(sub, "lxml")
                emails |= _extract_emails(sub, sub_soup)
                kind = _page_kind(url)
                if kind in ("people", "contact") and len(res.team_excerpt) < TEAM_EXCERPT_MAX:
                    page_text = _text_of(sub_soup, TEAM_EXCERPT_PER_PAGE)
                    res.team_excerpt = (res.team_excerpt + "\n---\n" + page_text if res.team_excerpt else page_text)[:TEAM_EXCERPT_MAX]
                elif kind == "impressum" and not res.impressum_excerpt:
                    res.impressum_excerpt = _text_of(sub_soup, 2500)
    except Exception as exc:  # network errors, TLS, etc.
        res.error = f"{type(exc).__name__}: {exc}"[:300]

    res.emails = rank_emails(emails, res.domain, place_hint, prefer_generic=prefer_generic, own_domain_only=own_domain_only)
    res.best_email = res.emails[0] if res.emails else None
    return res


def _make_client(timeout: float, headers: dict) -> httpx.Client:
    """Redirects are followed by hand in _fetch (every hop is checked). Tests patch this."""
    return httpx.Client(timeout=timeout, follow_redirects=False, headers=headers)


def _fetch(client: httpx.Client, url: str, max_bytes: int) -> tuple[str | None, str]:
    """Return (html, final_url). html is None on failure.

    Every URL (and every redirect target, at most MAX_REDIRECTS hops) is checked with
    check_url and requested at the resolved IP with the Host header set, so the crawler
    cannot be pointed at the LAN. Only text/html bodies up to max_bytes are read."""
    final = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            host, port, ip = check_url(final)
            req_headers = {"Host": host if port in (80, 443) else f"{host}:{port}"}
            with client.stream("GET", _pinned(final, host, port, ip), headers=req_headers, extensions={"sni_hostname": host}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    final = urljoin(final, r.headers["location"]).split("#")[0]
                    continue
                if r.status_code != 200:
                    return None, final
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in HTML_TYPES:
                    return None, final
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    return None, final
                chunks = []
                size = 0
                for chunk in r.iter_bytes():
                    if size + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - size])
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
                enc = r.encoding or "utf-8"
                return raw.decode(enc, errors="replace"), final
        log.debug("fetch %s: zu viele Weiterleitungen", url)
        return None, final
    except UnsafeURL as exc:
        log.info("fetch %s abgelehnt: %s", final, exc)
        return None, final
    except Exception as exc:
        log.debug("fetch %s failed: %s", final, exc)
        return None, final


def _page_kind(url: str) -> str:
    path = urlparse(url).path.lower()
    if any(h in path for h in PEOPLE_HINTS):
        return "people"
    if "kontakt" in path or "contact" in path:
        return "contact"
    if "impressum" in path or "imprint" in path or "mentions" in path or "legal" in path:
        return "impressum"
    return "other"


def _contact_links(soup: BeautifulSoup, base: str, budget: int = 4) -> list[str]:
    """Same-host links that look like team/about/contact/Impressum pages, most useful first:
    people pages (management before team before about; two, or one if budget pages leave
    no room for more), one contact page, the Impressum, then the rest. A site with many
    staff or "kontakt-…" pages must not crowd out the others."""
    base_host = urlparse(base).hostname or ""
    seen: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text(" ", strip=True) or "").lower()
        hay = (href.lower() + " " + label)
        if not any(h in hay for h in CONTACT_HINTS):
            continue
        full = urljoin(base, href)
        if not full.startswith(("http://", "https://")):
            continue
        if (urlparse(full).hostname or "") != base_host:
            continue
        full = full.split("#")[0]
        if full not in seen:
            seen.append(full)
    def people_rank(u: str) -> int:
        path = urlparse(u).path.lower()
        return next(i for i, h in enumerate(PEOPLE_HINTS) if h in path)

    people = sorted((u for u in seen if _page_kind(u) == "people"), key=people_rank)  # stable
    # the general contact page ("/kontakt") before brand or department ones ("/marke/kontakt-neuwagen")
    contact = sorted((u for u in seen if _page_kind(u) == "contact"), key=lambda u: len(urlparse(u).path))
    impressum = [u for u in seen if _page_kind(u) == "impressum"]
    other = [u for u in seen if _page_kind(u) == "other"]
    n_people = min(PEOPLE_PAGES_FIRST, max(1, budget - 1)) if contact else PEOPLE_PAGES_FIRST
    first = people[:n_people] + contact[:1] + impressum[:1]
    return first + [u for u in people + contact + impressum + other if u not in first]
