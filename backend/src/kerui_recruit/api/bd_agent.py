from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import BdEvidence, BdLead, BdSearchSession

router = APIRouter(prefix="/api/bd/agent", tags=["bd_agent"])


class AgentQueryRequest(BaseModel):
    query: str = Field(min_length=1)
    kind: str = Field(default="text", pattern="^(text|candidate|jd)$")
    limit: int = Field(default=10, ge=1, le=100)


class FollowUpRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)


class EvidenceResponse(BaseModel):
    claim: str | None
    quote: str | None
    source_url: str | None


class AgentLeadResponse(BaseModel):
    id: str
    source: str
    company_name: str
    job_title: str | None
    posted_time: str | None
    salary_range: str | None
    level: str | None
    requirements: list[str] = []
    summary: str | None
    url: str | None
    status: str
    confidence: float | None
    is_hiring: bool | None
    evidence: list[EvidenceResponse] = []


class AgentQueryResponse(BaseModel):
    session_id: str
    leads: list[AgentLeadResponse]


@router.post("/query", response_model=AgentQueryResponse)
async def run_agent(
    command: AgentQueryRequest, request: Request
) -> AgentQueryResponse:
    services: AppServices = request.app.state.services
    result = await services.bd_agent.run(
        command.query, kind=command.kind, limit=command.limit
    )
    return _to_response(result.session_id, result.leads, services)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/query-stream")
async def query_stream(
    command: AgentQueryRequest, request: Request
) -> StreamingResponse:
    services: AppServices = request.app.state.services
    agent = services.bd_agent

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()

        async def run_agent() -> None:
            result = await agent.run(
                command.query,
                kind=command.kind,
                limit=command.limit,
                progress=queue,
            )
            await queue.put(("__result__", result))

        task = asyncio.create_task(run_agent())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, tuple):
                    response = _to_response(
                        item[1].session_id, item[1].leads, services
                    )
                    yield _sse("result", {"type": "result", **response.model_dump()})
                    break
                yield _sse("progress", {"type": "progress", **item})
        finally:
            await task

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/session/{session_id}/follow-up", response_model=AgentQueryResponse)
async def follow_up(
    session_id: str, command: FollowUpRequest, request: Request
) -> AgentQueryResponse:
    services: AppServices = request.app.state.services
    result = await services.bd_agent.follow_up(session_id, command.query, limit=command.limit)
    return _to_response(result.session_id, result.leads, services)


@router.get("/session/{session_id}/export")
def export_report(session_id: str, request: Request) -> Response:
    services: AppServices = request.app.state.services
    markdown = _build_report(services, session_id)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bd_report_{session_id}.md"'
        },
    )


def _to_response(
    session_id: str, leads: list[BdLead], services: AppServices
) -> AgentQueryResponse:
    encryption = services.encryption_service
    return AgentQueryResponse(
        session_id=session_id,
        leads=[
            AgentLeadResponse(
                id=lead.id,
                source=lead.source,
                company_name=encryption.decrypt(lead.company_name),
                job_title=(
                    encryption.decrypt(lead.job_title) if lead.job_title else None
                ),
                posted_time=lead.posted_time,
                salary_range=lead.salary_range,
                level=lead.level,
                requirements=lead.requirements or [],
                summary=lead.raw_snippet,
                url=lead.url,
                status=lead.status,
                confidence=lead.confidence,
                is_hiring=lead.is_hiring,
                evidence=_evidence_items(lead),
            )
            for lead in leads
        ],
    )


def _evidence_items(lead: BdLead) -> list[EvidenceResponse]:
    try:
        items = list(lead.evidence)
    except Exception:
        items = []
    return [
        EvidenceResponse(claim=e.claim, quote=e.quote, source_url=e.source_url)
        for e in items
    ]


def _build_report(services: AppServices, session_id: str) -> str:
    encryption = services.encryption_service
    with services.session_factory() as session:
        search_session = session.get(BdSearchSession, session_id)
        if search_session is None:
            raise LookupError(f"BdSearchSession not found: {session_id}")
        leads = list(
            session.scalars(
                select(BdLead).where(BdLead.session_id == session_id)
            ).all()
        )
        evidence_by_lead: dict[str, list[BdEvidence]] = {}
        for lead in leads:
            evidence_by_lead[lead.id] = list(
                session.scalars(
                    select(BdEvidence).where(BdEvidence.lead_id == lead.id)
                ).all()
            )

    lines = [f"# BD 线索报告", "", f"查询：{search_session.query}", ""]
    for lead in leads:
        company = encryption.decrypt(lead.company_name)
        job_title = encryption.decrypt(lead.job_title) if lead.job_title else "—"
        hiring = {True: "在招", False: "不在招", None: "未知"}[lead.is_hiring]
        lines.append(f"## {company} · {job_title}")
        lines.append(f"- 是否在招：{hiring}")
        if lead.confidence is not None:
            lines.append(f"- 置信度：{lead.confidence:.0%}")
        if lead.raw_snippet:
            lines.append(f"- 摘要：{lead.raw_snippet}")
        if lead.url:
            lines.append(f"- 链接：{lead.url}")
        evidence = evidence_by_lead.get(lead.id, [])
        if evidence:
            lines.append("- 证据：")
            for item in evidence:
                quote = (item.quote or "").strip()
                lines.append(f"  - {quote}（{item.source_url or '无来源'}）")
        lines.append("")
    return "\n".join(lines)
