from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import (
    Candidate,
    CandidateContact,
    Jd,
    JdRevision,
    MatchResult,
    MatchRun,
    ResumeRevision,
    TaskRecord,
)
from kerui_recruit.search.contracts import resolve_search_status
from kerui_recruit.tasks.repository import TaskSpec


router = APIRouter(prefix="/api/match", tags=["match"])


class MatchJdRequest(BaseModel):
    revision_id: str = Field(min_length=1)
    limit: int = Field(default=1000, ge=1, le=5000)
    mode: str = Field(default="hybrid", pattern="^(keyword|vector|hybrid)$")


class MatchItem(BaseModel):
    candidate_id: str
    revision_id: str
    name: str = ""
    phone: str | None = None
    parsed_data: dict | None = None
    original_filename: str | None = None
    content: str
    score: float
    matched_channels: tuple[str, ...]
    total_years: float | None
    highest_degree: str | None
    location: str | None
    result_id: str | None = None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    eligibility: str = "eligible"
    match_tier: str = "needs_review"
    # 职业方向一致性的可读说明（如「职业细分命中 1/2」）。多值方向本身从 parsed_data 读取，
    # 不再冗余一份顶层字段：candidate 的 career_directions / business_directions 已在其中。
    direction_reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class MatchResponse(BaseModel):
    run_id: str | None
    items: list[MatchItem]
    status: str = "success"
    empty_reason: str | None = None
    degraded_reasons: list[str] = Field(default_factory=list)


class ReverseMatchItem(BaseModel):
    jd_id: str
    revision_id: str
    company: str
    title: str
    score: float


@router.post("/jd", response_model=MatchResponse)
async def match_jd(command: MatchJdRequest, request: Request) -> MatchResponse:
    services: AppServices = request.app.state.services
    page = await services.match_service.match_jd(
        revision_id=command.revision_id,
        limit=command.limit,
        mode=command.mode,
    )
    status = resolve_search_status(page.items, page.empty_reason, page.degraded_reasons)
    # index_not_ready / service_error 不应写入看似正常的空 match_run。
    recorded = None
    if status not in ("index_not_ready", "service_error"):
        recorded = services.match_service.record_run(
            revision_id=command.revision_id,
            hits=page.items,
            mode=command.mode,
        )
    candidate_ids = [hit.candidate_id for hit in page.items]
    revision_ids = [hit.revision_id for hit in page.items]
    with services.session_factory() as session:
        names: dict[str, str] = {}
        phones: dict[str, str | None] = {}
        if candidate_ids:
            for candidate_id, display_name, phone_encrypted in session.execute(
                select(Candidate.id, Candidate.display_name, CandidateContact.phone_encrypted)
                .outerjoin(CandidateContact, CandidateContact.candidate_id == Candidate.id)
                .where(Candidate.id.in_(candidate_ids))
            ).all():
                names[candidate_id] = display_name
                phones[candidate_id] = phone_encrypted
        revisions = (
            {
                revision.id: revision
                for revision in session.scalars(
                    select(ResumeRevision).where(ResumeRevision.id.in_(revision_ids))
                ).all()
            }
            if revision_ids
            else {}
        )
    encryption = services.encryption_service

    def _item(hit) -> MatchItem:
        match_score = services.match_service.score(command.revision_id, hit)
        return MatchItem(
            candidate_id=hit.candidate_id,
            revision_id=hit.revision_id,
            name=names.get(hit.candidate_id, hit.candidate_id),
            phone=(
                encryption.decrypt(phones.get(hit.candidate_id))
                if encryption is not None and phones.get(hit.candidate_id)
                else None
            ),
            parsed_data=(
                revisions[hit.revision_id].parsed_data
                if hit.revision_id in revisions
                else None
            ),
            original_filename=(
                revisions[hit.revision_id].original_filename
                if hit.revision_id in revisions
                else None
            ),
            content=hit.content,
            score=match_score.total,
            matched_channels=hit.matched_channels,
            total_years=hit.total_years,
            highest_degree=hit.highest_degree,
            location=hit.location,
            result_id=recorded.result_ids.get(hit.candidate_id) if recorded else None,
            matched_skills=list(match_score.matched_skills),
            missing_skills=list(match_score.missing_skills),
            eligibility=match_score.eligibility,
            match_tier=match_score.match_tier,
            direction_reason=match_score.direction_reason,
            evidence=list(match_score.breakdown.get("duty_evidence") or []),
        )

    return MatchResponse(
        run_id=recorded.run_id if recorded else None,
        status=status,
        empty_reason=page.empty_reason,
        degraded_reasons=list(page.degraded_reasons),
        items=[_item(hit) for hit in page.items],
    )


@router.get("/run/{run_id}/export")
def export_match_run(run_id: str, request: Request) -> Response:
    services: AppServices = request.app.state.services
    xlsx_bytes = services.export_service.export_match_run(run_id)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="match_{run_id}.xlsx"'},
    )


class BatchMatchRequest(BaseModel):
    revision_ids: list[str] = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    mode: str = Field(default="hybrid", pattern="^(keyword|vector|hybrid)$")


class BatchMatchResult(BaseModel):
    revision_id: str
    run_id: str
    items: list[MatchItem]


class BatchMatchResponse(BaseModel):
    results: list[BatchMatchResult]


@router.post("/batch", response_model=BatchMatchResponse)
async def match_batch(command: BatchMatchRequest, request: Request) -> BatchMatchResponse:
    services: AppServices = request.app.state.services
    results: list[BatchMatchResult] = []
    for revision_id in command.revision_ids:
        page = await services.match_service.match_jd(
            revision_id=revision_id,
            limit=command.limit,
            mode=command.mode,
        )
        recorded = services.match_service.record_run(
            revision_id=revision_id,
            hits=page.items,
            mode=command.mode,
        )
        results.append(
            BatchMatchResult(
                revision_id=revision_id,
                run_id=recorded.run_id,
                items=[
                    MatchItem(
                        candidate_id=hit.candidate_id,
                        revision_id=hit.revision_id,
                        content=hit.content,
                        score=services.match_service.score(revision_id, hit).total,
                        matched_channels=hit.matched_channels,
                        total_years=hit.total_years,
                        highest_degree=hit.highest_degree,
                        location=hit.location,
                        result_id=recorded.result_ids.get(hit.candidate_id),
                    )
                    for hit in page.items
                ],
            )
        )
    return BatchMatchResponse(results=results)


class MarkResultRequest(BaseModel):
    status: str = Field(pattern="^(未处理|保留)$")


class MarkResultResponse(BaseModel):
    result_id: str
    status: str


@router.post("/result/{result_id}/mark", response_model=MarkResultResponse)
def mark_result(result_id: str, command: MarkResultRequest, request: Request) -> MarkResultResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session, session.begin():
        result = session.get(MatchResult, result_id)
        if result is None:
            raise ApiError(404, "E_MATCH_RESULT_NOT_FOUND", "匹配结果不存在")
        result.status = command.status
    return MarkResultResponse(result_id=result_id, status=command.status)


@router.get("/reverse/{candidate_id}", response_model=list[ReverseMatchItem])
async def reverse_match(
    candidate_id: str,
    request: Request,
    mode: str = Query(default="hybrid", pattern="^(keyword|vector|hybrid)$"),
) -> list[ReverseMatchItem]:
    services: AppServices = request.app.state.services
    matches = await services.scheduler_service.reverse_match_candidate(candidate_id, mode=mode)
    return [
        ReverseMatchItem(
            jd_id=m.jd_id,
            revision_id=m.revision_id,
            company=m.company,
            title=m.title,
            score=m.score,
        )
        for m in matches
    ]


class MatchResultItem(BaseModel):
    result_id: str
    candidate_id: str
    name: str
    score: float
    status: str
    total_years: float | None
    highest_degree: str | None
    location: str | None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)


class MatchResultGroup(BaseModel):
    jd_id: str
    revision_id: str
    company: str
    title: str
    items: list[MatchResultItem]


class MatchResultsResponse(BaseModel):
    groups: list[MatchResultGroup]


@router.get("/results", response_model=MatchResultsResponse)
def list_match_results(
    request: Request,
    limit_per_jd: int = Query(20, ge=1, le=100),
) -> MatchResultsResponse:
    """Group persisted match results by JD, keeping the top-k candidates each."""
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        results = session.scalars(
            select(MatchResult)
            .join(MatchRun, MatchResult.run_id == MatchRun.id)
            .order_by(MatchRun.created_at.desc(), MatchResult.total_score.desc())
        ).all()

        groups: dict[str, MatchResultGroup] = {}
        seen: dict[str, set[str]] = {}
        for result in results:
            revision_id = result.jd_revision_id
            if revision_id is None:
                continue

            group = groups.get(revision_id)
            if group is None:
                revision = session.get(JdRevision, revision_id)
                if revision is None:
                    continue
                jd = session.get(Jd, revision.jd_id)
                group = MatchResultGroup(
                    jd_id=revision.jd_id,
                    revision_id=revision_id,
                    company=jd.company if jd else "—",
                    title=jd.title if jd else "—",
                    items=[],
                )
                groups[revision_id] = group
                seen[revision_id] = set()

            if result.candidate_id in seen[revision_id]:
                continue
            if len(group.items) >= limit_per_jd:
                continue
            seen[revision_id].add(result.candidate_id)

            candidate = session.get(Candidate, result.candidate_id)
            revision = (
                session.get(ResumeRevision, result.resume_revision_id)
                if result.resume_revision_id
                else None
            )
            breakdown = result.score_breakdown or {}
            group.items.append(
                MatchResultItem(
                    result_id=result.id,
                    candidate_id=result.candidate_id,
                    name=candidate.display_name if candidate else result.candidate_id,
                    score=float(result.total_score or 0),
                    status=result.status,
                    total_years=(
                        float(candidate.total_years)
                        if candidate and candidate.total_years is not None
                        else None
                    ),
                    highest_degree=candidate.highest_degree if candidate else None,
                    location=(
                        (revision.parsed_data or {}).get("location")
                        if revision and revision.parsed_data
                        else None
                    ),
                    matched_skills=breakdown.get("matched_skills") or [],
                    missing_skills=breakdown.get("missing_skills") or [],
                )
            )
    return MatchResultsResponse(groups=list(groups.values()))


@router.get("/jd/{revision_id}/export")
def export_match_jd(revision_id: str, request: Request) -> Response:
    services: AppServices = request.app.state.services
    xlsx_bytes = services.export_service.export_match_jd(revision_id)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="match_{revision_id}.xlsx"'},
    )


class CreateCaseFromResultResponse(BaseModel):
    case_id: str
    result_id: str
    status: str


@router.post("/result/{result_id}/create-case", response_model=CreateCaseFromResultResponse)
def create_case_from_result(result_id: str, request: Request) -> CreateCaseFromResultResponse:
    """Turn a persisted match result into a recruitment case and link it back."""
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        result = session.get(MatchResult, result_id)
        if result is None:
            raise ApiError(404, "E_MATCH_RESULT_NOT_FOUND", "匹配结果不存在")
        revision = (
            session.get(JdRevision, result.jd_revision_id)
            if result.jd_revision_id
            else None
        )
        if revision is None:
            raise ApiError(409, "E_MATCH_RESULT_NO_JD", "匹配结果缺少岗位信息")
        candidate_id = result.candidate_id
        jd_id = revision.jd_id

    case = services.case_service.create(candidate_id=candidate_id, jd_id=jd_id)

    with services.session_factory() as session, session.begin():
        result = session.get(MatchResult, result_id)
        result.case_id = case.id
        result.status = "保留"
    return CreateCaseFromResultResponse(
        case_id=case.id, result_id=result_id, status="保留"
    )


class CandidateMatchItem(BaseModel):
    result_id: str
    jd_id: str
    revision_id: str
    # 该次匹配实际使用的简历修订 ID（人找岗位反向匹配时 revision_id 是 JD 修订 ID，
    # 打开原始简历需另用此字段，避免把 JD 修订误当作简历）。
    resume_revision_id: str | None = None
    company: str
    title: str
    score: float
    status: str
    case_id: str | None
    jd_status: str = "OPEN"
    ai_category: str | None = None
    parsed_data: dict | None = None
    source_text: str | None = None


class CandidateMatchResponse(BaseModel):
    run_id: str | None
    items: list[CandidateMatchItem]


@router.get("/candidate/{candidate_id}", response_model=list[CandidateMatchItem])
def list_candidate_matches(candidate_id: str, request: Request) -> list[CandidateMatchItem]:
    """Return persisted matches for one candidate, deduplicated by JD."""
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        results = session.scalars(
            select(MatchResult)
            .join(MatchRun, MatchResult.run_id == MatchRun.id)
            .where(MatchResult.candidate_id == candidate_id)
            .order_by(MatchRun.created_at.desc(), MatchResult.total_score.desc())
        ).all()

        items: list[CandidateMatchItem] = []
        seen_jd: set[str] = set()
        for result in results:
            revision_id = result.jd_revision_id
            if revision_id is None:
                continue
            revision = session.get(JdRevision, revision_id)
            if revision is None:
                continue
            if revision.jd_id in seen_jd:
                continue
            seen_jd.add(revision.jd_id)
            jd = session.get(Jd, revision.jd_id)
            items.append(
                CandidateMatchItem(
                    result_id=result.id,
                    jd_id=revision.jd_id,
                    revision_id=revision_id,
                    resume_revision_id=result.resume_revision_id,
                    company=jd.company if jd else "—",
                    title=jd.title if jd else "—",
                    score=float(result.total_score or 0),
                    status=result.status,
                    case_id=result.case_id,
                )
            )
    return items


@router.post("/candidate/{candidate_id}", response_model=CandidateMatchResponse)
async def match_candidate(
    candidate_id: str,
    request: Request,
    mode: str = Query(default="hybrid", pattern="^(keyword|vector|hybrid)$"),
) -> CandidateMatchResponse:
    """Run and persist a candidate-driven reverse match, returning the matched JDs."""
    services: AppServices = request.app.state.services
    records = await services.match_service.reverse_match_candidate(candidate_id, mode=mode)
    recorded = services.match_service.record_reverse_run(
        candidate_id=candidate_id,
        records=records,
        mode=mode,
    )
    items = _candidate_match_items(services, records, recorded.result_ids)
    return CandidateMatchResponse(run_id=recorded.run_id, items=items)


def _candidate_match_items(services, records, result_ids: dict[str, str]) -> list[CandidateMatchItem]:
    """把一次反向匹配的记录装配成岗位行；``result_id`` 是建流程/标记的凭据。"""
    if not records:
        return []
    with services.session_factory() as session:
        jds = {
            jd.id: jd
            for jd in session.scalars(
                select(Jd).where(Jd.id.in_([record.jd_id for record in records]))
            ).all()
        }
        revisions = {
            revision.id: revision
            for revision in session.scalars(
                select(JdRevision).where(
                    JdRevision.id.in_([record.revision_id for record in records])
                )
            ).all()
        }
    items: list[CandidateMatchItem] = []
    for record in records:
        total = (record.score or services.match_service.score(record.revision_id, record.hit)).total
        jd = jds.get(record.jd_id)
        revision = revisions.get(record.revision_id)
        items.append(
            CandidateMatchItem(
                result_id=result_ids.get(record.revision_id) or "",
                jd_id=record.jd_id,
                revision_id=record.revision_id,
                resume_revision_id=record.hit.revision_id,
                company=record.company,
                title=record.title,
                score=total,
                status="未处理",
                case_id=None,
                jd_status=jd.status if jd else "OPEN",
                ai_category=revision.ai_category if revision else None,
                parsed_data=revision.parsed_data if revision else None,
                source_text=revision.source_text if revision else None,
            )
        )
    return items


class BulkCandidateMatchRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=200)
    mode: str = Field(default="hybrid", pattern="^(keyword|vector|hybrid)$")


class BulkCandidateMatchItem(BaseModel):
    candidate_id: str
    name: str | None = None
    run_id: str | None = None
    matched_jobs: int = 0
    error: str | None = None
    items: list[CandidateMatchItem] = Field(default_factory=list)


class JdMatchDetail(BaseModel):
    """批量结果里按 JD 修订去重后的岗位详情，避免 N 份候选人重复携带同一份 parsed_data。"""

    revision_id: str
    jd_id: str
    company: str
    title: str
    jd_status: str = "OPEN"
    ai_category: str | None = None
    parsed_data: dict | None = None
    source_text: str | None = None


class BulkCandidateMatchResponse(BaseModel):
    results: list[BulkCandidateMatchItem]
    jd_details: list[JdMatchDetail] = Field(default_factory=list)


def _candidate_display_name(services, candidate_id: str) -> str | None:
    with services.session_factory() as session:
        candidate = session.get(Candidate, candidate_id)
    return candidate.display_name if candidate else None


@router.post("/candidates/bulk", response_model=BulkCandidateMatchResponse)
async def bulk_match_candidates(
    command: BulkCandidateMatchRequest,
    request: Request,
) -> BulkCandidateMatchResponse:
    """批量人找岗位：逐人执行一次基础反向匹配并持久化，不自动触发 AI 深度复核。

    返回逐人的命中岗位行；岗位详情（parsed_data/source_text）按 JD 修订去重后
    放在 ``jd_details`` 里，前端合并即可复用单人抽屉的岗位展示。
    """
    services: AppServices = request.app.state.services
    results: list[BulkCandidateMatchItem] = []
    for candidate_id in dict.fromkeys(command.candidate_ids):
        try:
            records = await services.match_service.reverse_match_candidate(
                candidate_id, mode=command.mode
            )
            recorded = services.match_service.record_reverse_run(
                candidate_id=candidate_id, records=records, mode=command.mode
            )
            results.append(BulkCandidateMatchItem(
                candidate_id=candidate_id,
                name=_candidate_display_name(services, candidate_id),
                run_id=recorded.run_id,
                matched_jobs=len(records),
                items=_candidate_match_items(services, records, recorded.result_ids),
            ))
        except Exception as exc:  # noqa: BLE001 - per-candidate failure must not abort batch
            results.append(BulkCandidateMatchItem(
                candidate_id=candidate_id,
                name=_candidate_display_name(services, candidate_id),
                matched_jobs=0,
                error=type(exc).__name__,
            ))
    return BulkCandidateMatchResponse(
        results=results,
        jd_details=_jd_match_details(services, results),
    )


def _jd_match_details(services, results: list[BulkCandidateMatchItem]) -> list[JdMatchDetail]:
    """按 JD 修订去重，装配批量结果的岗位详情。"""
    revision_ids = list(dict.fromkeys(
        item.revision_id for result in results for item in result.items if item.revision_id
    ))
    if not revision_ids:
        return []
    with services.session_factory() as session:
        rows = session.execute(
            select(JdRevision, Jd)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.id.in_(revision_ids))
        ).all()
        return [
            JdMatchDetail(
                revision_id=revision.id,
                jd_id=jd.id,
                company=jd.company or "—",
                title=jd.title or "—",
                jd_status=jd.status,
                ai_category=revision.ai_category,
                parsed_data=revision.parsed_data,
                source_text=revision.source_text,
            )
            for revision, jd in rows
        ]


class AiReviewStartResponse(BaseModel):
    review_id: str
    status: str


class AiReviewStartRequest(BaseModel):
    reasoning: bool = False


REVIEW_TERMINAL_STATUSES = ("SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED")


def _latest_review_task(session, run_id: str) -> TaskRecord | None:
    """取该 run 最新一次复核任务（幂等键带 ``:{旧任务 id}`` 后缀，必须按前缀查）。"""
    return session.scalar(
        select(TaskRecord)
        .where(TaskRecord.idempotency_key.like(f"MATCH_REVIEW:{run_id}%"))
        .order_by(TaskRecord.created_at.desc(), TaskRecord.id.desc())
        .limit(1)
    )


@router.post("/run/{run_id}/ai-review", response_model=AiReviewStartResponse)
def start_ai_review(run_id: str, request: Request, body: AiReviewStartRequest = Body(default_factory=AiReviewStartRequest)) -> AiReviewStartResponse:
    """启动一次 AI 深度复核任务（默认关闭思考，用户手动触发；基础结果不受影响）。

    重复点击的行为：进行中 → 返回同一个任务（幂等）；失败/死信 → 原地重排同一条任务；
    SUCCESS/CANCELLED → **换新幂等键**重新入队，否则 ``enqueue`` 会把旧任务原样还回来，
    表现为「点了没反应」。
    """
    services: AppServices = request.app.state.services
    if services.match_review_service is None:
        raise ApiError(503, "E_MATCH_REVIEW_UNAVAILABLE", "AI 复核服务未配置")
    prefix = f"MATCH_REVIEW:{run_id}"
    with services.session_factory() as session:
        latest = _latest_review_task(session, run_id)
        latest_id, latest_status = (latest.id, latest.status) if latest is not None else (None, None)
    if latest_id is not None and latest_status not in REVIEW_TERMINAL_STATUSES:
        return AiReviewStartResponse(review_id=latest_id, status=latest_status or "QUEUED")
    if latest_id is not None and latest_status in ("FAILED", "DEAD_LETTER"):
        # 失败终态：原地重排，前端手里的 review_id 依然有效。
        services.task_repository.retry(latest_id)
        return AiReviewStartResponse(review_id=latest_id, status="QUEUED")
    key = prefix if latest_id is None else f"{prefix}:{latest_id}"
    task_id = services.task_repository.enqueue(TaskSpec(
        task_type="MATCH_REVIEW",
        queue_name="batch",
        priority=5,
        payload={"run_id": run_id, "reasoning": body.reasoning},
        idempotency_key=key,
    ))
    return AiReviewStartResponse(review_id=task_id, status="QUEUED")


@router.get("/run/{run_id}/ai-review")
def get_ai_review(run_id: str, request: Request) -> dict:
    """查询一次 AI 复核任务的进度与结果（not_started/running/completed/partial/failed/cancelled）。"""
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        task = _latest_review_task(session, run_id)
        if task is None:
            return {"status": "not_started", "progress": 0, "result_ref": None, "error_message": None}
        return {
            "status": task.status,
            "progress": task.progress,
            "result_ref": task.result_ref,
            "error_message": task.error_message,
        }
