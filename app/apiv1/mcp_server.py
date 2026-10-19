"""MCP transport (Streamable HTTP under /mcp). Same tools and rules as the REST API.

The bearer token is checked by security.ApiGuard before a request reaches the MCP app;
the tools read the authenticated principal from the HTTP request scope."""

from __future__ import annotations

import json
from typing import Annotated
from urllib.parse import urlparse

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import __version__
from ..config import get_settings
from . import schemas, service
from .service import ApiError, Call

INSTRUCTIONS = """Outreach: B2B-Mailkampagnen pro Mandant.
Ablauf: list_tenants -> create_campaign -> import_leads -> set_sequence -> preview_email / send_test -> start_campaign -> get_stats.
Alle schreibenden Werkzeuge liefern ohne confirm=true nur eine Vorschau und ändern nichts.
start_campaign braucht zusätzlich confirm_send=true. Schritte sind 1-basiert.
Platzhalter in Betreff und Text: {{anrede}}, {{vorname}}, {{nachname}}, {{firma}}, {{ort}}, {{email}}. body_text ist Klartext, kein HTML."""

mcp = MCPServer(name="outreach", version=__version__, instructions=INSTRUCTIONS)


def _call(ctx: Context) -> Call:
    request = ctx.request_context.request
    state = request.scope.get("state", {}) if request is not None else {}
    principal = state.get("principal")
    if principal is None:  # ApiGuard did not run: refuse rather than guess
        raise ToolError(json.dumps({"error": {"code": "unauthorized", "message": "Unauthorized"}}))
    return Call(principal=principal, transport="mcp", client_ip=state.get("client_ip"))


def _run(ctx: Context, tool: str, fn, write: bool = False) -> dict:
    try:
        return service.execute(tool, _call(ctx), fn, write)
    except ApiError as exc:
        raise ToolError(json.dumps(exc.body(), ensure_ascii=False))


def _model(cls, **kwargs):
    try:
        return cls(**kwargs)
    except ValidationError as exc:
        raise ToolError(
            json.dumps({"error": {"code": "validation_error", "message": "Ungültige Eingabe", "details": schemas.validation_details(exc)}}, ensure_ascii=False)
        )


Confirm = Annotated[bool, Field(description="false = nur Vorschau, true = ausführen")]


# Argument shapes for the tool schemas. Deliberately loose: the strict models in
# schemas.py validate inside the tool, so errors come back as structured JSON.
class LeadArg(BaseModel):
    model_config = ConfigDict(extra="allow")
    firma: str
    email: str
    anrede: str | None = Field(None, description="Wird unverändert für {{anrede}} eingesetzt, z.B. 'Guten Tag Herr Muster'")
    vorname: str | None = None
    nachname: str | None = None
    ort: str | None = None
    notiz: str | None = None
    sprache: str | None = Field(None, description="ISO-639-1, z.B. de")


class StepArg(BaseModel):
    model_config = ConfigDict(extra="allow")
    subject: str
    body_text: str = Field(description="Klartext, kein HTML; Platzhalter {{anrede}} {{vorname}} {{nachname}} {{firma}} {{ort}} {{email}}")
    delay_days: int = Field(0, description="Tage nach dem vorherigen Schritt; erster Schritt 0")


@mcp.tool(description="Mandanten, auf die der Token Zugriff hat.")
def list_tenants(ctx: Context) -> dict:
    return _run(ctx, "list_tenants", service.list_tenants)


@mcp.tool(description="Kampagnen der erlaubten Mandanten, optional gefiltert nach Mandanten-Slug.")
def list_campaigns(ctx: Context, tenant: str | None = None) -> dict:
    return _run(ctx, "list_campaigns", lambda db, call: service.list_campaigns(db, call, tenant=tenant))


@mcp.tool(description="Details einer Kampagne inkl. Sequenz und Lead-Zahlen.")
def get_campaign(ctx: Context, campaign_id: int) -> dict:
    return _run(ctx, "get_campaign", lambda db, call: service.get_campaign(db, call, campaign_id))


@mcp.tool(description="Neue Kampagne (Status draft) mit eigener Absenderidentität anlegen.")
def create_campaign(
    ctx: Context,
    name: str,
    reply_to: str,
    sender_name: str,
    sender_email: str,
    tenant: str | None = None,
    language: str = "de",
    auto_approve: bool = False,
    confirm: Confirm = False,
) -> dict:
    data = _model(schemas.CampaignCreate, tenant=tenant, name=name, reply_to=reply_to, sender_name=sender_name,
                  sender_email=sender_email, language=language, auto_approve=auto_approve, confirm=confirm)
    return _run(ctx, "create_campaign", lambda db, call: service.create_campaign(db, call, data), write=True)


@mcp.tool(description="Leads in eine Kampagne importieren. Duplikate (gleiche E-Mail beim Mandanten, doppelt in der Liste oder gesperrt) werden gemeldet und nicht angelegt.")
def import_leads(ctx: Context, campaign_id: int, leads: list[LeadArg], confirm: Confirm = False) -> dict:
    data = _model(schemas.LeadImport, leads=[l.model_dump(exclude_unset=True) for l in leads], confirm=confirm)
    return _run(ctx, "import_leads", lambda db, call: service.import_leads(db, call, campaign_id, data), write=True)


@mcp.tool(description="Leads einer Kampagne, optional nach Status gefiltert.")
def list_leads(ctx: Context, campaign_id: int, status: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    return _run(ctx, "list_leads", lambda db, call: service.list_leads(db, call, campaign_id, status, limit, offset))


@mcp.tool(description="Felder eines Leads ändern (nur angegebene Felder, leerer String leert ein Feld). Offene Entwürfe werden verworfen und neu erzeugt.")
def update_lead(
    ctx: Context,
    lead_id: int,
    firma: str | None = None,
    anrede: str | None = None,
    vorname: str | None = None,
    nachname: str | None = None,
    email: str | None = None,
    ort: str | None = None,
    notiz: str | None = None,
    sprache: str | None = None,
    confirm: Confirm = False,
) -> dict:
    given = {k: v for k, v in dict(firma=firma, anrede=anrede, vorname=vorname, nachname=nachname, email=email, ort=ort, notiz=notiz, sprache=sprache).items() if v is not None}
    data = _model(schemas.LeadUpdate, confirm=confirm, **given)
    return _run(ctx, "update_lead", lambda db, call: service.update_lead(db, call, lead_id, data), write=True)


@mcp.tool(description="Lead löschen (Soft-Delete): ausgeblendet, keine weiteren Mails.")
def delete_lead(ctx: Context, lead_id: int, confirm: Confirm = False) -> dict:
    data = _model(schemas.Confirm, confirm=confirm)
    return _run(ctx, "delete_lead", lambda db, call: service.delete_lead(db, call, lead_id, data), write=True)


@mcp.tool(description="Sequenz der Kampagne setzen: Schritte mit subject, body_text (Klartext) und delay_days. Ersetzt die bisherige Sequenz.")
def set_sequence(ctx: Context, campaign_id: int, steps: list[StepArg], confirm: Confirm = False) -> dict:
    data = _model(schemas.SequenceSet, steps=[s.model_dump() for s in steps], confirm=confirm)
    return _run(ctx, "set_sequence", lambda db, call: service.set_sequence(db, call, campaign_id, data), write=True)


@mcp.tool(description="Fertig gerenderte Mail für einen Lead und Schritt (1-basiert), ohne zu senden.")
def preview_email(ctx: Context, campaign_id: int, lead_id: int, step: int = 1) -> dict:
    return _run(ctx, "preview_email", lambda db, call: service.preview_email(db, call, campaign_id, lead_id, step))


@mcp.tool(description="Testmail eines Schritts an eine interne Adresse senden (Domain-Allowlist).")
def send_test(ctx: Context, campaign_id: int, to: str, step: int = 1, lead_id: int | None = None, confirm: Confirm = False) -> dict:
    data = _model(schemas.SendTest, to=to, step=step, lead_id=lead_id, confirm=confirm)
    return _run(ctx, "send_test", lambda db, call: service.send_test(db, call, campaign_id, data), write=True)


@mcp.tool(description="Kampagne starten (Versand beginnt). Braucht confirm=true UND confirm_send=true.")
def start_campaign(ctx: Context, campaign_id: int, confirm: Confirm = False, confirm_send: bool = False) -> dict:
    data = _model(schemas.StartCampaign, confirm=confirm, confirm_send=confirm_send)
    return _run(ctx, "start_campaign", lambda db, call: service.start_campaign(db, call, campaign_id, data), write=True)


@mcp.tool(description="Kampagne pausieren.")
def pause_campaign(ctx: Context, campaign_id: int, confirm: Confirm = False) -> dict:
    data = _model(schemas.Confirm, confirm=confirm)
    return _run(ctx, "pause_campaign", lambda db, call: service.pause_campaign(db, call, campaign_id, data), write=True)


@mcp.tool(description="Kampagne beenden (Status done).")
def stop_campaign(ctx: Context, campaign_id: int, confirm: Confirm = False) -> dict:
    data = _model(schemas.Confirm, confirm=confirm)
    return _run(ctx, "stop_campaign", lambda db, call: service.stop_campaign(db, call, campaign_id, data), write=True)


@mcp.tool(description="Kennzahlen: gesendet, Bounces, Abmeldungen, Antworten (aus dem Zammad-Abgleich).")
def get_stats(ctx: Context, campaign_id: int) -> dict:
    return _run(ctx, "get_stats", lambda db, call: service.get_stats(db, call, campaign_id))


def allowed_hosts() -> list[str]:
    s = get_settings()
    hosts = [h.strip() for h in s.outreach_mcp_allowed_hosts.split(",") if h.strip()]
    if not hosts:
        host = urlparse(s.public_base_url).hostname
        hosts = [host] if host else []
    # health checks and local tests inside the container
    return hosts + [h + ":*" for h in hosts] + ["127.0.0.1:*", "localhost:*"]


def http_app():
    """Starlette app serving /mcp. Origin is only checked when a browser sends one."""
    hosts = allowed_hosts()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["https://" + h for h in hosts],
    )
    return mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security)
