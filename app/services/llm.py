"""Claude: write the outreach mail for a lead, extract the contact person, classify replies."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from ..config import get_settings
from ..models import Campaign, Lead, Tenant

log = logging.getLogger(__name__)

LANG_NAMES = {"de": "Deutsch (Schweizer Schreibweise: ss statt ß)", "fr": "Französisch", "it": "Italienisch", "en": "Englisch"}


@dataclass
class Draft:
    subject: str
    body: str
    contact_name: str | None
    salutation: str | None


def _client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")
    return anthropic.Anthropic(api_key=key)


class NoJsonError(ValueError):
    """The model answered, but not with a usable JSON object."""


# room for the model's reasoning plus the JSON; 1200 cut answers off mid-JSON
MAX_TOKENS = 4000


def _json_from(text: str) -> dict:
    """First JSON object in the text; tolerates code fences and text around it."""
    text = re.sub(r"```(?:json)?", "", text or "")
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise NoJsonError("Keine JSON-Antwort vom Modell")


def _text_of(msg) -> str:
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")


def _excerpt(text: str, n: int = 300) -> str:
    return " ".join((text or "").split())[:n]


def write_mail(tenant: Tenant, campaign: Campaign, lead: Lead, step: int, previous: list[tuple[str, str]]) -> Draft:
    """previous: [(subject, body)] of mails already sent to this lead."""
    steps = campaign.steps or [{"delay_days": 0, "instruction": ""}]
    step_cfg = steps[min(step, len(steps) - 1)]
    lang = LANG_NAMES.get(campaign.language, campaign.language)

    system = (
        "Du schreibst kurze, persönliche B2B-Erstkontakt-Mails für ein Schweizer KMU. "
        "Keine Floskeln, kein Marketing-Sprech, keine Superlative, kein Betreff mit Clickbait. "
        "Maximal 120 Wörter im Text. Du-/Sie-Form: Sie. Keine Platzhalter, keine eckigen Klammern. "
        "Verwende nur Fakten aus den gelieferten Infos; erfinde nichts über den Empfänger. "
        "Die Mail soll so klingen, als hätte der Absender die Webseite des Empfängers wirklich angeschaut. "
        "Signatur und Fusszeile werden separat angehängt, schreibe keine Grussformel mit Namen und keine Signatur. "
        "Antworte ausschliesslich mit einem JSON-Objekt."
    )

    prev_txt = ""
    if previous:
        prev_txt = "\n\nBereits gesendete Mails an diesen Empfänger (Follow-up darauf aufbauen, nicht wiederholen):\n" + "\n---\n".join(
            f"Betreff: {s}\n{b}" for s, b in previous
        )

    user = f"""Sprache: {lang}
Absenderfirma: {tenant.name}
Was die Absenderfirma anbietet: {tenant.company_blurb}

Kampagne: {campaign.name}
Zielgruppe: {campaign.target_description}
Angebot / Nutzen für diese Zielgruppe: {campaign.offer}
Tonalität: {campaign.tone or 'sachlich, freundlich, auf Augenhöhe'}
Schritt {step + 1} von {len(steps)}. Anweisung für diesen Schritt: {step_cfg.get('instruction') or ('Erstkontakt' if step == 0 else 'Kurzes, freundliches Follow-up')}

Empfänger-Firma: {lead.company}
Adresse: {lead.address or '-'}
Webseite: {lead.website or '-'}
Google-Kategorien: {lead.place_types or '-'}
Bekannter Ansprechpartner: {lead.contact_name or 'unbekannt'}
Empfängeradresse: {lead.email or '-'}
Vorgegebene Anrede: {lead.salutation or 'keine'}

Auszug Startseite:
{(lead.site_excerpt or '')[:2500]}

Auszug Impressum/Kontakt:
{(lead.impressum_excerpt or '')[:2000]}
{prev_txt}

Aufgabe:
1. Finde im Impressum/Kontakt-Auszug, falls vorhanden, den Namen der Inhaberin/des Inhabers bzw. der Geschäftsführung (Vor- und Nachname). Wenn unklar: null.
2. Ist eine Anrede vorgegeben, übernimm sie unverändert. Sonst bilde sie aus dem bekannten Ansprechpartner bzw. dem gefundenen Namen (z.B. "Guten Tag Herr Müller" oder "Guten Tag Frau Keller"); passt der Name nicht sicher zur Empfängeradresse, kein Name oder Geschlecht unklar: "Guten Tag".
3. Schreibe Betreff und Mailtext. Der Text beginnt mit der Anrede.

JSON-Format:
{{"contact_name": string|null, "salutation": string, "subject": string, "body": string}}"""

    client = _client()
    model = get_settings().anthropic_model
    messages = [{"role": "user", "content": user}]
    msg = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system, messages=messages)
    text = _text_of(msg)
    try:
        data = _json_from(text)
    except NoJsonError:
        # one corrective retry; keep what the model said so the error is diagnosable
        first_stop, first_text = getattr(msg, "stop_reason", None), text
        log.warning("write_mail lead %s: keine JSON-Antwort (stop_reason=%s): %s", lead.id, first_stop, _excerpt(first_text))
        if text.strip():
            messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": "Bitte antworte jetzt ausschliesslich mit dem JSON-Objekt im verlangten Format, ohne weiteren Text. "
            "Fehlen Angaben zum Empfänger, schreibe trotzdem eine allgemeine Mail ohne erfundene Details.",
        })
        msg = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system, messages=messages)
        text = _text_of(msg)
        try:
            data = _json_from(text)
        except NoJsonError:
            raise NoJsonError(
                f"Keine JSON-Antwort vom Modell (stop_reason={getattr(msg, 'stop_reason', None)}): "
                f"{_excerpt(text) or _excerpt(first_text) or '(leer)'}"
            ) from None
    return Draft(
        subject=str(data.get("subject", "")).strip()[:200],
        body=str(data.get("body", "")).strip(),
        contact_name=(data.get("contact_name") or None),
        salutation=(data.get("salutation") or None),
    )


REPLY_CLASSES = ("interested", "not_interested", "question", "out_of_office", "auto_reply", "unsubscribe", "bounce", "other")


def classify_reply(subject: str, body: str) -> str:
    system = (
        "Du klassifizierst Antworten auf B2B-Erstkontakt-Mails. Antworte nur mit einem Wort aus dieser Liste: "
        + ", ".join(REPLY_CLASSES)
        + ". interested = will mehr wissen / Termin / Offerte; question = Rückfrage; not_interested = Absage; "
        "unsubscribe = will keine Mails mehr; out_of_office = Abwesenheitsnotiz; auto_reply = automatische Bestätigung; "
        "bounce = Unzustellbarkeitsmeldung; other = Rest."
    )
    msg = _client().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=20,
        system=system,
        messages=[{"role": "user", "content": f"Betreff: {subject}\n\n{body[:3000]}"}],
    )
    return parse_reply_class(_text_of(msg))


def parse_reply_class(text: str) -> str:
    """Map the model's answer to a class. Whole-word match, longest name first:
    a substring check took "not_interested" for "interested"."""
    words = re.findall(r"[a-z_]+", (text or "").lower().replace("-", "_"))
    for c in sorted(REPLY_CLASSES, key=len, reverse=True):
        if c in words:
            return c
    return "other"
