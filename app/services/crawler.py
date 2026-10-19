"""Fetch a company website, find Impressum/Kontakt pages and extract e-mail addresses.

Swiss companies have to publish a contact address in the Impressum, so this
finds a usable mailbox for most SMEs without any paid data source.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import get_settings

log = logging.getLogger(__name__)

CONTACT_HINTS = ("impressum", "kontakt", "contact", "imprint", "ueber-uns", "über-uns", "about", "team", "mentions-legales", "legal")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
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


def _extract_emails(html: str, soup: BeautifulSoup) -> set[str]:
    found: set[str] = set()
    for a in soup.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        addr = href[7:].split("?")[0].strip()
        if addr:
            found.add(addr.lower())
    for m in EMAIL_RE.findall(html):
        found.add(m.lower())
    for m in OBFUSCATED_RE.findall(html):
        found.add(f"{m[0]}@{m[1]}.{m[2]}".lower())
    clean = set()
    for e in found:
        if e.endswith(SKIP_EMAIL_SUFFIX) or any(p in e for p in SKIP_EMAIL_PARTS):
            continue
        if len(e) > 100 or e.count("@") != 1:
            continue
        clean.add(e.strip(".,;:"))
    return clean


def is_generic(email: str) -> bool:
    return email.partition("@")[0].split("+")[0].lower() in GENERIC_LOCALPARTS


def rank_emails(emails: set[str], domain: str | None) -> list[str]:
    """Own-domain first. A single personal address on the company domain beats info@;
    with several personal addresses we cannot tell who is who, so info@ comes first."""
    def same(e: str) -> bool:
        dom = e.partition("@")[2]
        return bool(domain and (dom == domain or dom.endswith("." + domain)))

    own = [e for e in emails if same(e)]
    other = [e for e in emails if not same(e)]
    own_personal = sorted(e for e in own if not is_generic(e))
    own_generic = sorted((e for e in own if is_generic(e)), key=lambda e: (e.split("@")[0] != "info", e))
    if len(own_personal) == 1:
        ordered = own_personal + own_generic
    else:
        ordered = own_generic + own_personal
    return ordered + sorted(other, key=lambda e: (not is_generic(e), e))


def pick_for_contact(candidates: list[str], contact_name: str | None) -> str | None:
    """If a candidate's local part matches the contact's name, return it."""
    if not contact_name or not candidates:
        return None
    parts = [p.lower() for p in re.split(r"[\s\-]+", contact_name) if len(p) > 2]
    if not parts:
        return None
    for e in candidates:
        local = e.partition("@")[0].lower()
        if any(p in local for p in parts):
            return e
    return None


def crawl(website: str) -> CrawlResult:
    s = get_settings()
    res = CrawlResult(domain=domain_of(website))
    if not website:
        res.error = "keine Webseite"
        return res
    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    headers = {"User-Agent": s.crawl_user_agent, "Accept-Language": "de-CH,de;q=0.9,en;q=0.5"}
    emails: set[str] = set()
    try:
        with httpx.Client(timeout=s.crawl_timeout, follow_redirects=True, headers=headers) as client:
            html, base = _fetch(client, website, s.crawl_max_bytes)
            res.pages_fetched += 1
            if html is None:
                res.error = "Startseite nicht abrufbar"
                return res
            res.domain = domain_of(base) or res.domain
            soup = BeautifulSoup(html, "lxml")
            emails |= _extract_emails(html, soup)
            links = _contact_links(soup, base)
            res.site_excerpt = _text_of(BeautifulSoup(html, "lxml"), 3000)

            for url in links[:4]:
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

    res.emails = rank_emails(emails, res.domain)
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


def _contact_links(soup: BeautifulSoup, base: str) -> list[str]:
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
    # Impressum first, it has the names.
    seen.sort(key=lambda u: 0 if ("impressum" in u.lower() or "imprint" in u.lower()) else 1)
    return seen
