"""Turn an incoming mail body into readable plain text and split off the quoted history."""

from __future__ import annotations

import re

_BLOCK_TAGS = ("p", "div", "li", "tr", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "hr")

# first line of the quoted original in common clients (de/fr/it/en)
_QUOTE_START = re.compile(
    r"^\s*(?:"
    r"(?:Von|From|De|Da)\s*:\s*\S.*"
    r"|-{2,}\s*(?:Original|Ursprüngliche|Message d'origine|Messaggio originale).*"
    r"|(?:Am|On|Le|Il)\s.+\b(?:schrieb|wrote|a écrit|ha scritto)\b.*"
    r"|>.*"
    r")$",
    re.IGNORECASE,
)


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return normalize(soup.get_text(""))


def normalize(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_quote(text: str) -> tuple[str, str]:
    """(own reply, quoted history). Without a recognisable quote the whole text is the reply."""
    lines = (text or "").split("\n")
    for i, ln in enumerate(lines):
        if i > 0 and _QUOTE_START.match(ln):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return (text or "").strip(), ""


def body_from_article(article: dict) -> str:
    body = article.get("body") or ""
    if (article.get("content_type") or "").startswith("text/html"):
        return html_to_text(body)
    return normalize(body)
