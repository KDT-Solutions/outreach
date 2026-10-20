"""Fetch a company website, find Impressum/Kontakt pages and extract e-mail addresses.

Swiss companies have to publish a contact address in the Impressum, so this
finds a usable mailbox for most SMEs without any paid data source.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import get_settings

log = logging.getLogger(__name__)

CONTACT_HINTS = (
    "impressum", "kontakt", "contact", "imprint", "ueber-uns", "über-uns", "about", "team", "mentions-legales", "legal",
    "geschaeftsleitung", "geschäftsleitung", "geschaftsleitung", "inhaber", "management", "direction",
)
# Pages that name the owner or management: fetched right after the Impressum
MANAGEMENT_HINTS = ("geschaeftsleitung", "geschäftsleitung", "geschaftsleitung", "inhaber", "management", "direction", "team", "ueber-uns", "über-uns")
# no "%": URL-encoded mailto links ("%20info@...") must not leak into the address
EMAIL_RE = re.compile(r"[A-Z0-9._+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
STRICT_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$")
OBFUSCATED_RE = re.compile(
    r"([A-Z0-9._%+\-]+)\s*(?:\(at\)|\[at\]|\{at\}|\s@\s|&#64;|\(ät\)|\[ät\])\s*([A-Z0-9.\-]+)\s*(?:\(dot\)|\[dot\]|\.)\s*([A-Z]{2,})",
    re.I,
)
SKIP_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
SKIP_EMAIL_PARTS = ("example.", "sentry", "wixpress", "wordpress", "domain.", "email.com", "noreply", "no-reply", "@2x", "u003e")
GENERIC_LOCALPARTS = ("info", "kontakt", "contact", "office", "mail", "hello", "hallo", "welcome", "admin", "sekretariat", "empfang", "praxis", "shop", "verkauf", "sales", "support")


@dataclass
class CrawlResult:
    domain: str | None = None
    emails: list[str] = field(default_factory=list)
    best_email: str | None = None
    site_excerpt: str = ""
    impressum_excerpt: str = ""
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
        with httpx.Client(timeout=timeout or s.crawl_timeout, follow_redirects=True, headers=headers) as client:
            html, base = _fetch(client, website, s.crawl_max_bytes)
            res.pages_fetched += 1
            if html is None:
                res.error = "Startseite nicht abrufbar"
                return res
            res.domain = domain_of(base) or res.domain
            soup = BeautifulSoup(html, "lxml")
            emails |= _extract_emails(html, soup)
            links = _contact_links(soup, base, contact_first=prefer_generic)
            res.site_excerpt = _text_of(BeautifulSoup(html, "lxml"), 3000)

            for url in links[:max(0, subpages)]:
                sub, _ = _fetch(client, url, s.crawl_max_bytes)
                if sub is None:
                    continue
                res.pages_fetched += 1
                sub_soup = BeautifulSoup(sub, "lxml")
                emails |= _extract_emails(sub, sub_soup)
                if any(h in url.lower() for h in ("impressum", "imprint", "kontakt", "contact", "mentions")) and not res.impressum_excerpt:
                    res.impressum_excerpt = _text_of(sub_soup, 2500)
    except Exception as exc:  # network errors, TLS, etc.
        res.error = f"{type(exc).__name__}: {exc}"[:300]

    res.emails = rank_emails(emails, res.domain, place_hint, prefer_generic=prefer_generic, own_domain_only=own_domain_only)
    res.best_email = res.emails[0] if res.emails else None
    return res


def _fetch(client: httpx.Client, url: str, max_bytes: int) -> tuple[str | None, str]:
    """Return (html, final_url). html is None on failure."""
    try:
        with client.stream("GET", url) as r:
            final = str(r.url)
            if r.status_code != 200:
                return None, final
            ctype = r.headers.get("content-type", "")
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None, final
            chunks = []
            size = 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    break
            raw = b"".join(chunks)
            enc = r.encoding or "utf-8"
            return raw.decode(enc, errors="replace"), final
    except Exception as exc:
        log.debug("fetch %s failed: %s", url, exc)
        return None, url


def _contact_links(soup: BeautifulSoup, base: str, contact_first: bool = False) -> list[str]:
    """Same-host links that look like Impressum/contact/about pages, most useful first.
    contact_first: Kontakt before management pages (when generic mailboxes are wanted)."""
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
    # Impressum first (it names the owner), then management/team pages (their mailboxes),
    # then contact pages; a site with many "kontakt-…" pages must not crowd them out.
    def prio(u: str) -> int:
        path = urlparse(u).path.lower()
        if "impressum" in path or "imprint" in path or "mentions" in path:
            return 0
        if contact_first and ("kontakt" in path or "contact" in path):
            return 1
        if any(h in path for h in MANAGEMENT_HINTS):
            return 2
        return 3

    seen.sort(key=prio)
    return seen
