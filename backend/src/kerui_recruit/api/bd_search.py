from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import BdLead


router = APIRouter(prefix="/api/bd", tags=["bd_search"])


class SearchLeadsRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)


class LeadResponse(BaseModel):
    id: str
    source: str
    company_name: str
    job_title: str | None
    raw_snippet: str | None
    url: str | None
    status: str


class SearchForCandidateRequest(BaseModel):
    candidate_id: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)


class UpdateLeadStatusRequest(BaseModel):
    status: str = Field(pattern="^(新线索|已联系|已定级|已存档)$")
    note: str | None = None


class PoolCandidateResponse(BaseModel):
    candidate_id: str
    name: str
    phone: str | None
    revision_id: str


@router.post("/search", response_model=list[LeadResponse])
def search_leads(command: SearchLeadsRequest, request: Request) -> list[LeadResponse]:
    services: AppServices = request.app.state.services
    leads = services.bd_search_service.search_leads(query=command.query, limit=command.limit)
    return _leads_to_response(leads, services)


@router.post("/search-for-candidate", response_model=list[LeadResponse])
def search_for_candidate(
    command: SearchForCandidateRequest, request: Request
) -> list[LeadResponse]:
    services: AppServices = request.app.state.services
    leads = services.bd_search_service.search_leads_for_candidate(
        command.candidate_id, limit=command.limit
    )
    return _leads_to_response(leads, services)


@router.post("/{lead_id}/status", response_model=LeadResponse)
def update_lead_status(
    lead_id: str, command: UpdateLeadStatusRequest, request: Request
) -> LeadResponse:
    services: AppServices = request.app.state.services
    lead = services.bd_search_service.update_status(
        lead_id, command.status, note=command.note
    )
    encryption = services.encryption_service
    return LeadResponse(
        id=lead.id,
        source=lead.source,
        company_name=encryption.decrypt(lead.company_name),
        job_title=encryption.decrypt(lead.job_title) if lead.job_title else None,
        raw_snippet=lead.raw_snippet,
        url=lead.url,
        status=lead.status,
    )


@router.post("/leads/{lead_id}/lookup-pool", response_model=list[PoolCandidateResponse])
def lookup_pool(lead_id: str, request: Request) -> list[PoolCandidateResponse]:
    """反查人才库：找到最近两段经历至少一段属于该线索公司的候选人。"""
    services: AppServices = request.app.state.services
    encryption = services.encryption_service
    with services.session_factory() as session:
        lead = session.get(BdLead, lead_id)
        if lead is None:
            raise LookupError(f"BdLead not found: {lead_id}")
        company = encryption.decrypt(lead.company_name)
    matches = services.bd_search_service.match_company_in_pool(company)
    return [PoolCandidateResponse(**item) for item in matches]


def _leads_to_response(leads, services) -> list[LeadResponse]:
    encryption = services.encryption_service
    return [
        LeadResponse(
            id=lead.id,
            source=lead.source,
            company_name=encryption.decrypt(lead.company_name),
            job_title=encryption.decrypt(lead.job_title) if lead.job_title else None,
            raw_snippet=lead.raw_snippet,
            url=lead.url,
            status=lead.status,
        )
        for lead in leads
    ]