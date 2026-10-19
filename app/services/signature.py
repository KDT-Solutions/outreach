"""Signature and unsubscribe line under every mail.

Resolution, separately for the signature block and the unsubscribe text:
1. campaign override, 2. tenant signature in the lead's language, 3. tenant signature in
the campaign language, 4. the tenant's default (signature + footer from the tenant form,
German unsubscribe text). The unsubscribe link is always present: if the signature does
not place {{unsubscribe_url}} itself, "<unsubscribe text> <url>" is appended."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import get_settings
from ..models import Campaign, Lead
from . import templating

DEFAULT_UNSUBSCRIBE_TEXT = "Keine weiteren Mails von uns gewünscht? Hier abmelden:"


@dataclass
class Resolved:
    body: str
    body_source: str  # campaign | tenant:<lang> | default
    unsubscribe_text: str
    unsubscribe_source: str


def unsubscribe_url(lead: Lead) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/u/{lead.unsubscribe_token}"


def resolve(campaign: Campaign, lead: Lead) -> Resolved:
    tenant = campaign.tenant
    by_lang = {s.language: s for s in tenant.signatures}
    langs = [l for l in dict.fromkeys(((lead.language or "").lower(), (campaign.language or "").lower())) if l]

    body, body_source = None, None
    if (campaign.signature_override or "").strip():
        body, body_source = campaign.signature_override.strip(), "campaign"
    else:
        for lang in langs:
            sig = by_lang.get(lang)
            if sig and sig.body_text.strip():
                body, body_source = sig.body_text.strip(), f"tenant:{lang}"
                break
    if body is None:
        body = "\n\n".join(p for p in (tenant.signature.strip(), tenant.footer.strip()) if p)
        body_source = "default"

    text, text_source = None, None
    if (campaign.unsubscribe_text_override or "").strip():
        text, text_source = campaign.unsubscribe_text_override.strip(), "campaign"
    else:
        for lang in langs:
            sig = by_lang.get(lang)
            if sig and sig.unsubscribe_text.strip():
                text, text_source = sig.unsubscribe_text.strip(), f"tenant:{lang}"
                break
    if text is None:
        text, text_source = DEFAULT_UNSUBSCRIBE_TEXT, "default"
    return Resolved(body, body_source, text, text_source)


def render(campaign: Campaign, lead: Lead) -> tuple[str, Resolved]:
    """The block appended under the mail body, and how it was resolved."""
    r = resolve(campaign, lead)
    name, email, reply_to = campaign.sender
    url = unsubscribe_url(lead)
    values = {"sender_name": name, "sender_email": email, "reply_to": reply_to, "unsubscribe_url": url}
    block = templating.render(r.body, values).strip()
    if "unsubscribe_url" not in templating.placeholders_in(r.body):
        line = f"{templating.render(r.unsubscribe_text, values)} {url}"
        block = f"{block}\n\n{line}" if block else line
    return block, r


def compose(campaign: Campaign, lead: Lead, body: str) -> str:
    block, _ = render(campaign, lead)
    return f"{body.strip()}\n\n{block}"
