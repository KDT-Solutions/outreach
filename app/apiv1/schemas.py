"""Input models for the automation API. The same models validate REST bodies and MCP tool
arguments, so both transports reject the same input with the same messages."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..services import templating, verify

MAX_IMPORT = 1000
MAX_STEPS = 10


def _email(v: str) -> str:
    v = v.strip().lower()
    if len(v) > 200 or not verify.syntax_ok(v):
        raise ValueError("Ungültige E-Mail-Adresse")
    return v


def _single_line(v: str) -> str:
    v = v.strip()
    if "\n" in v or "\r" in v:
        raise ValueError("Zeilenumbrüche sind nicht erlaubt")
    return v


def _optional_line(v: str | None) -> str | None:
    if v is None:
        return None
    return _single_line(v) or None


def _template(v: str) -> str:
    problems = templating.check_template(v)
    if problems:
        raise ValueError("; ".join(problems))
    return v


def _signature_template(v: str | None) -> str | None:
    if v is None:
        return None
    problems = templating.check_template(v, allowed=templating.SIGNATURE_PLACEHOLDERS)
    if problems:
        raise ValueError("; ".join(problems))
    return v.strip()


Email = Annotated[str, AfterValidator(_email)]
# signature block (multi-line) and unsubscribe text (one line); "" clears a campaign override
SignatureText = Annotated[str, Field(max_length=5000), AfterValidator(_signature_template)]
UnsubscribeText = Annotated[str, Field(max_length=500), AfterValidator(_single_line), AfterValidator(_signature_template)]
Lang = Annotated[str, Field(pattern=r"^[a-z]{2}$", description="ISO-639-1, z.B. de, fr, it, en")]


def Line(max_length: int, min_length: int = 1):  # noqa: N802
    return Annotated[str, Field(min_length=min_length, max_length=max_length), AfterValidator(_single_line)]


def OptLine(max_length: int):  # noqa: N802
    return Annotated[str | None, Field(default=None, max_length=max_length), AfterValidator(_optional_line)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CampaignCreate(_Strict):
    tenant: str | None = Field(None, description="Mandanten-Slug; nur nötig, wenn der Token mehrere Mandanten hat")
    name: Line(120)
    sender_name: Line(120)
    sender_email: Email
    reply_to: Email
    language: Lang = "de"
    auto_approve: bool = Field(False, description="Gerenderte Mails ohne manuelle Freigabe in der Oberfläche senden (nur mit OUTREACH_ALLOW_AUTO_APPROVE=true)")
    signature_override: SignatureText | None = None
    unsubscribe_text_override: UnsubscribeText | None = None
    confirm: bool = False


class CampaignUpdate(_Strict):
    name: Line(120) | None = None
    sender_name: Line(120) | None = None
    sender_email: Email | None = None
    reply_to: Email | None = None
    auto_approve: bool | None = Field(None, description="true: wartende Entwürfe werden freigegeben und gehen in den Versand (nur mit OUTREACH_ALLOW_AUTO_APPROVE=true)")
    confirm: bool = False

    def changes(self) -> dict:
        return {k: v for k, v in self.model_dump(exclude_unset=True, exclude={"confirm"}).items() if v is not None}


class TenantSignatureSet(_Strict):
    tenant: str | None = Field(None, description="Mandanten-Slug; nur nötig, wenn der Token mehrere Mandanten hat")
    language: Lang
    body_text: Annotated[SignatureText, Field(min_length=1)]
    unsubscribe_text: Annotated[UnsubscribeText, Field(min_length=1)]
    confirm: bool = False


class SignatureBody(_Strict):
    """REST body for PUT /tenants/{tenant}/signatures/{language}; validated again as TenantSignatureSet."""

    body_text: str
    unsubscribe_text: str
    confirm: bool = False


class ApproveDrafts(_Strict):
    lead_ids: list[int] | None = Field(None, min_length=1, max_length=1000)
    all: bool = False
    confirm: bool = False

    @model_validator(mode="after")
    def _one_selector(self):
        if bool(self.lead_ids) == bool(self.all):
            raise ValueError("Entweder lead_ids oder all=true angeben")
        return self


class WaitCampaign(_Strict):
    reason: Line(500)
    confirm: bool = False


class _LeadSelection(_Strict):
    lead_ids: list[int] | None = Field(None, min_length=1, max_length=1000)
    all: bool = False
    confirm: bool = False

    @model_validator(mode="after")
    def _one_selector(self):
        if bool(self.lead_ids) == bool(self.all):
            raise ValueError("Entweder lead_ids oder all=true angeben")
        return self


class WaitLeads(_LeadSelection):
    reason: Line(500)


class ReleaseLeads(_LeadSelection):
    pass


class LeadWon(_Strict):
    note: Line(500) | None = None
    confirm: bool = False


class LeadIn(_Strict):
    firma: Line(200)
    anrede: OptLine(120) = None
    vorname: OptLine(80) = None
    nachname: OptLine(80) = None
    email: Email
    ort: OptLine(120) = None
    notiz: Annotated[str | None, Field(default=None, max_length=2000)] = None
    sprache: Lang | None = None


class LeadImport(_Strict):
    leads: list[LeadIn] = Field(min_length=1, max_length=MAX_IMPORT)
    confirm: bool = False


MAX_PROSPECT = 200


class LeadSearch(_Strict):
    query: Line(200)
    region: Line(120) = Field("Schweiz", description="Ort, Kanton, PLZ oder 'Schweiz'")
    radius_km: int = Field(25, ge=1, le=300)
    limit: int = Field(50, ge=1, le=MAX_PROSPECT)
    language: Lang = "de"
    confirm: bool = False


class EnrichEntry(_Strict):
    firma: Line(200)
    ort: OptLine(120) = None
    plz: Annotated[str | None, Field(default=None, pattern=r"^\d{4,5}$")] = None
    website: OptLine(300) = None


class LeadEnrich(_Strict):
    leads: list[EnrichEntry] = Field(min_length=1, max_length=MAX_PROSPECT)
    confirm: bool = False


class LeadUpdate(_Strict):
    firma: Line(200) | None = None
    anrede: OptLine(120) = None
    vorname: OptLine(80) = None
    nachname: OptLine(80) = None
    email: Email | None = None
    ort: OptLine(120) = None
    notiz: Annotated[str | None, Field(default=None, max_length=2000)] = None
    sprache: Lang | None = None
    confirm: bool = False

    def changes(self) -> dict:
        return self.model_dump(exclude_unset=True, exclude={"confirm"})


class StepIn(_Strict):
    subject: Annotated[Line(200), AfterValidator(_template)]
    body_text: Annotated[str, Field(min_length=1, max_length=10000), AfterValidator(_template)]
    delay_days: int = Field(0, ge=0, le=365, description="Tage nach dem vorherigen Schritt; beim ersten Schritt 0")


class SequenceSet(_Strict):
    steps: list[StepIn] = Field(min_length=1, max_length=MAX_STEPS)
    # None = unverändert, "" = Override entfernen
    signature_override: SignatureText | None = None
    unsubscribe_text_override: UnsubscribeText | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def _first_step_immediate(self):
        if self.steps[0].delay_days != 0:
            raise ValueError("Der erste Schritt muss delay_days 0 haben; der Versand beginnt mit dem Start der Kampagne")
        return self


class SendTest(_Strict):
    step: int = Field(1, ge=1, le=MAX_STEPS, description="Schritt, 1 = erste Mail")
    to: Email
    lead_id: int | None = Field(None, description="Lead, mit dessen Daten gerendert wird; ohne: Beispieldaten")
    confirm: bool = False


class Confirm(_Strict):
    confirm: bool = False


class StartCampaign(_Strict):
    confirm: bool = False
    confirm_send: bool = False


def validation_details(exc: ValidationError) -> list[dict]:
    """Pydantic errors without the echoed input (could be long or personal data)."""
    return [{"loc": [p for p in e["loc"]], "msg": e["msg"].removeprefix("Value error, ")} for e in exc.errors()]
