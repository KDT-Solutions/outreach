"""REST transport: /api/v1, JSON in and out. Auth happens in security.ApiGuard."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError

from . import prospect, schemas, service
from .service import Call

router = APIRouter()


def _call(request: Request) -> Call:
    return Call(principal=request.state.principal, transport="rest", client_ip=request.state.client_ip)


def _run(request: Request, tool: str, fn, write: bool = False):
    return service.execute(tool, _call(request), fn, write)


# ---------------------------------------------------------------- read
@router.get("/tenants", summary="list_tenants")
def list_tenants(request: Request):
    return _run(request, "list_tenants", service.list_tenants)


@router.get("/campaigns", summary="list_campaigns")
def list_campaigns(request: Request, tenant: str | None = None):
    return _run(request, "list_campaigns", partial(service.list_campaigns, tenant=tenant))


@router.get("/campaigns/{campaign_id}", summary="get_campaign")
def get_campaign(request: Request, campaign_id: int):
    return _run(request, "get_campaign", partial(service.get_campaign, campaign_id=campaign_id))


@router.get("/campaigns/{campaign_id}/leads", summary="list_leads")
def list_leads(request: Request, campaign_id: int, status: str | None = None, limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return _run(request, "list_leads", partial(service.list_leads, campaign_id=campaign_id, status=status, limit=limit, offset=offset))


@router.get("/campaigns/{campaign_id}/preview", summary="preview_email")
def preview_email(request: Request, campaign_id: int, lead_id: int, step: int = Query(1, ge=1)):
    return _run(request, "preview_email", partial(service.preview_email, campaign_id=campaign_id, lead_id=lead_id, step=step))


@router.get("/campaigns/{campaign_id}/stats", summary="get_stats")
def get_stats(request: Request, campaign_id: int):
    return _run(request, "get_stats", partial(service.get_stats, campaign_id=campaign_id))


# ---------------------------------------------------------------- write (preview unless confirm=true)
@router.post("/campaigns", summary="create_campaign")
def create_campaign(request: Request, body: schemas.CampaignCreate):
    return _run(request, "create_campaign", partial(service.create_campaign, data=body), write=True)


@router.post("/campaigns/{campaign_id}/leads/import", summary="import_leads")
def import_leads(request: Request, campaign_id: int, body: schemas.LeadImport):
    return _run(request, "import_leads", partial(service.import_leads, campaign_id=campaign_id, data=body), write=True)


@router.patch("/leads/{lead_id}", summary="update_lead")
def update_lead(request: Request, lead_id: int, body: schemas.LeadUpdate):
    return _run(request, "update_lead", partial(service.update_lead, lead_id=lead_id, data=body), write=True)


@router.post("/leads/{lead_id}/unsuppress", summary="unsuppress_lead")
def unsuppress_lead(request: Request, lead_id: int, body: schemas.Confirm):
    return _run(request, "unsuppress_lead", partial(service.unsuppress_lead, lead_id=lead_id, data=body), write=True)


@router.delete("/leads/{lead_id}", summary="delete_lead")
def delete_lead(request: Request, lead_id: int, confirm: bool = False):
    return _run(request, "delete_lead", partial(service.delete_lead, lead_id=lead_id, data=schemas.Confirm(confirm=confirm)), write=True)


@router.put("/campaigns/{campaign_id}/sequence", summary="set_sequence")
def set_sequence(request: Request, campaign_id: int, body: schemas.SequenceSet):
    return _run(request, "set_sequence", partial(service.set_sequence, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/send-test", summary="send_test")
def send_test(request: Request, campaign_id: int, body: schemas.SendTest):
    return _run(request, "send_test", partial(service.send_test, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/start", summary="start_campaign")
def start_campaign(request: Request, campaign_id: int, body: schemas.StartCampaign):
    return _run(request, "start_campaign", partial(service.start_campaign, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/pause", summary="pause_campaign")
def pause_campaign(request: Request, campaign_id: int, body: schemas.Confirm):
    return _run(request, "pause_campaign", partial(service.pause_campaign, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/stop", summary="stop_campaign")
def stop_campaign(request: Request, campaign_id: int, body: schemas.Confirm):
    return _run(request, "stop_campaign", partial(service.stop_campaign, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/wait", summary="wait_campaign")
def wait_campaign(request: Request, campaign_id: int, body: schemas.WaitCampaign):
    return _run(request, "wait_campaign", partial(service.wait_campaign, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/leads/wait", summary="wait_leads")
def wait_leads(request: Request, campaign_id: int, body: schemas.WaitLeads):
    return _run(request, "wait_leads", partial(service.wait_leads, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/leads/release", summary="release_leads")
def release_leads(request: Request, campaign_id: int, body: schemas.ReleaseLeads):
    return _run(request, "release_leads", partial(service.release_leads, campaign_id=campaign_id, data=body), write=True)


# ---------------------------------------------------------------- signatures, campaign settings, approval
@router.get("/tenants/{tenant}/signatures", summary="list_tenant_signatures")
def list_tenant_signatures(request: Request, tenant: str):
    return _run(request, "list_tenant_signatures", partial(service.list_tenant_signatures, tenant=tenant))


@router.put("/tenants/{tenant}/signatures/{language}", summary="set_tenant_signature")
def set_tenant_signature(request: Request, tenant: str, language: str, body: schemas.SignatureBody):
    try:
        data = schemas.TenantSignatureSet(tenant=tenant, language=language, **body.model_dump())
    except ValidationError as exc:
        raise service.ApiError(422, "validation_error", "Ungültige Eingabe", schemas.validation_details(exc))
    return _run(request, "set_tenant_signature", partial(service.set_tenant_signature, data=data), write=True)


@router.patch("/campaigns/{campaign_id}", summary="update_campaign")
def update_campaign(request: Request, campaign_id: int, body: schemas.CampaignUpdate):
    return _run(request, "update_campaign", partial(service.update_campaign, campaign_id=campaign_id, data=body), write=True)


@router.get("/campaigns/{campaign_id}/drafts", summary="list_drafts")
def list_drafts(request: Request, campaign_id: int, status: str = "draft", include_waiting: bool = False):
    return _run(request, "list_drafts", partial(service.list_drafts, campaign_id=campaign_id, status=status, include_waiting=include_waiting))


@router.post("/campaigns/{campaign_id}/drafts/approve", summary="approve_drafts")
def approve_drafts(request: Request, campaign_id: int, body: schemas.ApproveDrafts):
    return _run(request, "approve_drafts", partial(service.approve_drafts, campaign_id=campaign_id, data=body), write=True)


@router.post("/drafts/{draft_id}/reject", summary="reject_draft")
def reject_draft(request: Request, draft_id: int, body: schemas.Confirm):
    return _run(request, "reject_draft", partial(service.reject_draft, draft_id=draft_id, data=body), write=True)


# ---------------------------------------------------------------- finding leads
@router.post("/campaigns/{campaign_id}/leads/search", summary="search_leads")
def search_leads(request: Request, campaign_id: int, body: schemas.LeadSearch):
    return _run(request, "search_leads", partial(prospect.search_leads, campaign_id=campaign_id, data=body), write=True)


@router.post("/campaigns/{campaign_id}/leads/enrich", summary="enrich_leads")
def enrich_leads(request: Request, campaign_id: int, body: schemas.LeadEnrich):
    return _run(request, "enrich_leads", partial(prospect.enrich_leads, campaign_id=campaign_id, data=body), write=True)


# ---------------------------------------------------------------- audit log and delivery
@router.get("/audit", summary="list_audit_log")
def list_audit_log(request: Request, tenant: str | None = None, campaign_id: int | None = None, tool: str | None = None,
                   since: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return _run(request, "list_audit_log", partial(service.list_audit_log, tenant=tenant, campaign_id=campaign_id, tool=tool,
                                                    since=since, limit=limit, offset=offset))


@router.get("/campaigns/{campaign_id}/delivery", summary="get_delivery_status")
def get_delivery_status(request: Request, campaign_id: int):
    return _run(request, "get_delivery_status", partial(service.get_delivery_status, campaign_id=campaign_id))
