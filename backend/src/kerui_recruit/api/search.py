from dataclasses import asdict, replace
import json
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.providers.profile_pair import repaired_profile_view
from kerui_recruit.db.models import (
    Candidate,
    CandidateContact,
    IndexSyncRecord,
    ResumeDocument,
    ResumeRevision,
    SearchReview,
    TaskRecord,
)
from kerui_recruit.duplicates.service import normalize_phone
from kerui_recruit.resumes.normalize import normalize_gender
from kerui_recruit.schools.reference import SchoolReference
from kerui_recruit.search.contracts import CandidateFilters, resolve_search_status
from kerui_recruit.search.degrees import normalize_degree
from kerui_recruit.search.parse import PARSE_TIMEOUT_SECONDS
from kerui_recruit.search.query import ParsedQuery, has_skill, parse_query
from kerui_recruit.search.review import (
    describe_conditions,
    query_fingerprint,
    review_base_key,
)
from kerui_recruit.search.service import RELAXABLE_FIELDS, _blocking
from kerui_recruit.tasks.repository import TaskSpec


router = APIRouter(prefix="/api/search", tags=["search"])


class CandidateFiltersRequest(BaseModel):
    min_years: float | None = Field(default=None, ge=0, le=80)
    max_years: float | None = Field(default=None, ge=0, le=80)
    min_age: int | None = Field(default=None, ge=16, le=80)
    max_age: int | None = Field(default=None, ge=16, le=80)
    highest_degree: str | None = None
    degree_exact: bool = False
    location: str | None = None
    locations: list[str] = Field(default_factory=list)
    preferred_location: str | None = None
    preferred_locations: list[str] = Field(default_factory=list)
    candidate_status: str | None = "AVAILABLE"
    max_qs_rank: int | None = Field(default=None, ge=1)
    school_level: str | None = None
    exclude_skills: list[str] = Field(default_factory=list)
    phone: str | None = None
    gender: str | None = None
    communication_note: str | None = None
    name: str | None = None
    company: str | None = None
    companies: list[str] = Field(default_factory=list)  # 多值公司（OR），与面板手选同义
    title: str | None = None
    school: str | None = None
    direction: str | None = None
    school_region: str | None = None
    specializations: list[str] = Field(default_factory=list)
    career_directions: list[str] = Field(default_factory=list)
    career_specializations: list[str] = Field(default_factory=list)
    business_directions: list[str] = Field(default_factory=list)


class CandidateSearchRequest(BaseModel):
    query: str = Field(default="", max_length=2_000)
    mode: str = Field(default="hybrid")
    operator: Literal["smart", "and", "or"] = "smart"
    rewrite_enabled: bool = False
    search_body: bool = False  # 是否同时检索工作/项目经历正文：关键词与混合模式含义一致（只影响关键词通道，向量模式无 FTS 通道、不生效）
    parse_enabled: bool = False  # AI 智能解析：把输入框的自然语言拆成硬条件 + 词条 + 语义查询（默认关闭）
    filters: CandidateFiltersRequest = Field(default_factory=CandidateFiltersRequest)
    limit: int = Field(default=20, ge=1, le=2000)
    offset: int = Field(default=0, ge=0)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in ("keyword", "vector", "hybrid"):
            raise ValueError("mode 必须是 keyword / vector / hybrid 之一")
        return value

    @model_validator(mode="after")
    def _validate_combinations(self) -> "CandidateSearchRequest":
        if self.mode == "keyword" and self.rewrite_enabled:
            raise ValueError("关键词模式不支持 AI 语义改写（rewrite_enabled）")
        if self.mode in ("vector", "hybrid") and self.operator != "smart":
            raise ValueError("向量/混合模式仅支持智能排序（operator=smart）")
        return self


class CandidateSearchItem(BaseModel):
    candidate_id: str
    revision_id: str
    name: str
    phone: str | None
    reasons: list[str]
    parsed_data: dict | None
    content: str
    score: float
    matched_channels: tuple[str, ...]
    total_years: float | None
    highest_degree: str | None
    location: str | None
    qs_rank: int | None = None
    original_filename: str | None = None
    # 沟通记录：候选人级自由文本，**不在 parsed_data 里**，也不参与画像与索引。
    communication_note: str | None = None


class ParsedConditionView(BaseModel):
    field: str
    value: str
    confidence: str


class EffectiveConditionView(BaseModel):
    """**合并后**最终生效的硬条件：与面板手选并列展示，并标明来源。"""

    field: str
    value: str
    source: Literal["panel", "llm", "rule"]
    confidence: str


class QueryPlanResponse(BaseModel):
    operator: Literal["smart", "and", "or"]
    rewrite_requested: bool
    # 文档规定的六值枚举：unchanged / rejected 让调用方区分「无需改写」与「被校验拒绝」。
    rewrite_status: Literal["disabled", "not_applicable", "unchanged", "success", "rejected",
                            "unavailable"]
    semantic_query: str | None = None
    # 附加诊断：applied 表示改写向量是否真的参与召回；fallback_reason 只描述技术性原因。
    rewrite_applied: bool = False
    rewrite_fallback_reason: Literal["provider_error"] | None = None
    parsed_conditions: list[ParsedConditionView] = Field(default_factory=list)
    retained_keywords: str = ""
    # AI 解析回显：解析来源、交给 FTS 的词条、未识别残句、以及合并后的最终生效条件。
    parsed_plan_source: Literal["rule", "llm", "mixed"] = "rule"
    keyword_terms: str = ""
    unparsed_terms: list[str] = Field(default_factory=list)
    effective_conditions: list[EffectiveConditionView] = Field(default_factory=list)
    # 任务组 8：被判定「硬筛筛空」而退化为软排的条件（字段名）。非空表示这条查询的
    # 结果里，这些条件只参与打分、不再过滤——界面需要据此说明为什么结果比预期宽。
    relaxed_conditions: list[str] = Field(default_factory=list)


class CandidateSearchResponse(BaseModel):
    items: list[CandidateSearchItem]
    degraded_reasons: list[str]
    empty_reason: str | None = None
    status: str = "success"
    query_plan: QueryPlanResponse
    has_more: bool = False


def _search_deadline_budget(retrieval_budget: float, parse_enabled: bool) -> float:
    """这次搜索请求的总预算：检索一份，**开启 AI 智能解析时再加一份解析预算**。

    共用一份的后果实测过：解析要 10~18 秒（强制思考模型），会把 FTS / embedding / 重排的
    时间吃光，甚至把自己也拖超时——`PARSE_TIMEOUT_SECONDS = 2.5` 时代就是**每一次解析都超时**、
    静默回退规则链路。所以解析预算独立，且只在用户主动打开解析时才加。
    """
    return retrieval_budget + (PARSE_TIMEOUT_SECONDS if parse_enabled else 0.0)


@router.post("/candidates", response_model=CandidateSearchResponse)
async def search_candidates(
    command: CandidateSearchRequest,
    request: Request,
) -> CandidateSearchResponse:
    services: AppServices = request.app.state.services
    deadline = time.monotonic() + _search_deadline_budget(
        getattr(services.search_service, "search_timeout", 4.5), command.parse_enabled)
    query_plan = _query_plan(command)
    # 学校别名快照：查询概念解析与索引文档共用同一套标准名/别名映射（同一 deadline 内完成）。
    try:
        school_alias_groups = await _blocking(_school_alias_groups, services, deadline=deadline)
    except Exception:
        school_alias_groups = {}
    parsed = parse_query(command.query, school_alias_groups=school_alias_groups)
    extras: dict = {"source": "rule", "unparsed": (), "llm_fields": set(), "semantic": None}
    # AI 智能解析（默认关闭）：一次调用拆出 filters + keywords + semantic_query，逐字段校验后覆盖规则值。
    if command.parse_enabled and getattr(services, "query_parser", None) is not None:
        plan = await services.query_parser.parse(
            command.query, school_alias_groups=school_alias_groups, deadline_monotonic=deadline)
        parsed = ParsedQuery(keywords=plan.keywords, filters=plan.filters,
                             concepts=plan.concepts, conditions=plan.conditions)
        extras.update(source=plan.source, unparsed=plan.unparsed_terms,
                      llm_fields=set(plan.accepted_fields), semantic=plan.semantic_query)
    filters = _merge_filters(parsed.filters, command.filters)
    explicit_keys = set(command.filters.model_dump(exclude_unset=True))
    # 任务组 8：只有「AI 解析产出」且「使用者没在面板上重复填写」的条件才允许退化。
    # 面板手填的值意图明确，筛空就应该显示 0 结果，不能被悄悄放宽。
    relaxable = tuple(
        name for name in RELAXABLE_FIELDS
        if name in extras["llm_fields"] and name not in explicit_keys
    )
    # 回显「最终生效条件」：面板显式值优先，其余按 AI 解析 / 规则解析标注来源。
    extras["effective"] = _effective_conditions(
        filters, explicit_keys, extras["llm_fields"])
    query_plan = _query_plan(command, parsed, extras=extras)
    # Reserve a small part of the same budget for checking current SQLite facts.
    remaining = max(0., deadline - time.monotonic())
    # 学校别名解析：查询词与索引使用同一套标准名/别名映射，保证「北大」与「北京大学」一致。
    if filters.school:
        try:
            canonical = await _blocking(_resolve_school_query, services, filters.school, deadline=deadline)
            if canonical and canonical != filters.school:
                filters = replace(filters, school=canonical)
        except Exception:
            pass
    # 手机号/性别/沟通文本下沉：先在数据库层缩小候选人集合，再交给索引排序，不受向量召回上限影响。
    if filters.phone or filters.gender or filters.communication_note:
        try:
            ids = await _blocking(_resolve_structural_candidates, services, filters, deadline=deadline)
        except Exception:
            ids = None
        if ids is not None and not ids:
            return CandidateSearchResponse(
                items=[], degraded_reasons=[], empty_reason="no_match", status="no_match",
                query_plan=query_plan,
            )
        if ids:
            filters = replace(filters, candidate_ids=tuple(sorted(ids)))
    fetch_limit = min(command.offset + command.limit + 1, 5000)
    page = await services.search_service.search(
        parsed.keywords, filters, limit=fetch_limit, mode=command.mode,
        deadline=deadline - min(.25, remaining * .1),
        operator=command.operator,
        concepts=parsed.concepts,
        rewrite_enabled=command.rewrite_enabled,
        search_body=command.search_body,
        semantic_query=extras["semantic"],
        relaxable_fields=relaxable,
    )
    plan = _to_response_plan(page.query_plan, parsed, extras=extras) if getattr(page, "query_plan", None) is not None else query_plan
    if not page.items:
        return CandidateSearchResponse(
            items=[], degraded_reasons=list(page.degraded_reasons), empty_reason=page.empty_reason,
            status=resolve_search_status((), page.empty_reason, page.degraded_reasons),
            query_plan=plan,
        )
    try:
        # 用**实际生效**的条件做实时校验：若服务层已把某条判为「硬筛筛空」并退化为软排，
        # 这里必须跟着放宽，否则 `_hydrate_hits` 会把公司/职位再当硬条件执行一遍，
        # 把刚救回来的结果重新滤成 0。
        hydrate_filters = page.effective_filters or filters
        items, validation_reasons = await _blocking(_hydrate_hits, services, page.items, parsed.keywords, hydrate_filters, deadline=deadline)
    except Exception:
        return CandidateSearchResponse(
            items=[], degraded_reasons=list(page.degraded_reasons) + ["LIVE_VALIDATION_UNAVAILABLE"],
            empty_reason="service_error", status="service_error",
            query_plan=plan,
        )
    degraded = list(dict.fromkeys((*page.degraded_reasons, *validation_reasons)))
    has_more = len(items) > command.offset + command.limit
    items = items[command.offset:command.offset + command.limit]
    empty_reason = page.empty_reason if items else ("service_error" if degraded else "no_match")
    return CandidateSearchResponse(
        items=items, degraded_reasons=degraded, empty_reason=empty_reason,
        status=resolve_search_status(items, empty_reason, degraded),
        query_plan=plan, has_more=has_more,
    )


_PLAN_FIELD_LABELS = {
    "min_years": "最低年限", "max_years": "最高年限", "min_age": "最低年龄", "max_age": "最高年龄",
    "highest_degree": "最低学历", "locations": "现居城市", "preferred_locations": "意向城市",
    "max_qs_rank": "QS 排名", "school_level": "学校等级", "school_region": "属地",
    "exclude_skills": "排除", "phone": "手机号", "gender": "性别", "name": "姓名",
    "communication_note": "沟通文本",
    "company": "公司", "companies": "公司", "title": "职位", "school": "学校",
    "career_directions": "职业方向", "career_specializations": "职业细分",
    "business_directions": "业务方向",
}
# 不参与条件回显的内部字段：状态默认值、下沉集合、派生/重复字段。
_PLAN_HIDDEN_FIELDS = frozenset({
    "candidate_status", "candidate_ids", "evidence_terms", "degree_exact",
    "specializations", "location", "preferred_location", "direction",
})


def _effective_conditions(filters: CandidateFilters, explicit_keys: set[str],
                          llm_fields: set[str]) -> list[EffectiveConditionView]:
    """把**合并后**的最终条件渲染成回显项：面板 > AI 解析 > 规则解析，逐条标来源。"""
    views: list[EffectiveConditionView] = []
    for name, value in asdict(filters).items():
        if name in _PLAN_HIDDEN_FIELDS or value in (None, "", (), []):
            continue
        if name not in _PLAN_FIELD_LABELS:
            continue
        source = "panel" if name in explicit_keys else ("llm" if name in llm_fields else "rule")
        rendered = "、".join(str(item) for item in value) if isinstance(value, (tuple, list)) else str(value)
        views.append(EffectiveConditionView(
            field=name, value=rendered, source=source,
            confidence="explicit" if source == "panel" else "inferred",
        ))
    return views


def _to_response_plan(plan, parsed=None, *, extras: dict | None = None) -> QueryPlanResponse:
    """把服务层 QueryPlan（dataclass）映射为 API 响应模型，并合并解析条件与解析回显。"""
    extras = extras or {}
    return QueryPlanResponse(
        operator=plan.operator,
        rewrite_requested=plan.rewrite_requested,
        rewrite_status=plan.rewrite_status,
        semantic_query=plan.semantic_query,
        rewrite_applied=getattr(plan, "rewrite_applied", False),
        rewrite_fallback_reason=getattr(plan, "rewrite_fallback_reason", None),
        parsed_conditions=[
            ParsedConditionView(field=c.field, value=c.value, confidence=c.confidence)
            for c in (parsed.conditions if parsed else ())
        ],
        retained_keywords=parsed.keywords if parsed else "",
        parsed_plan_source=extras.get("source", "rule"),
        keyword_terms=parsed.keywords if parsed else "",
        unparsed_terms=list(extras.get("unparsed", ())),
        effective_conditions=list(extras.get("effective", ())),
        relaxed_conditions=list(getattr(plan, "relaxed", ())),
    )


def _query_plan(command: CandidateSearchRequest, parsed=None, *, extras: dict | None = None) -> QueryPlanResponse:
    """返回查询计划；服务层未回传 plan（如纯筛选提前返回）时的静态兜底。"""
    extras = extras or {}
    rewrite_status = "not_applicable" if command.mode == "keyword" else "disabled"
    return QueryPlanResponse(
        operator=command.operator,
        rewrite_requested=command.rewrite_enabled,
        rewrite_status=rewrite_status,
        semantic_query=None,
        parsed_conditions=[
            ParsedConditionView(field=c.field, value=c.value, confidence=c.confidence)
            for c in (parsed.conditions if parsed else ())
        ],
        retained_keywords=parsed.keywords if parsed else "",
        parsed_plan_source=extras.get("source", "rule"),
        keyword_terms=parsed.keywords if parsed else "",
        unparsed_terms=list(extras.get("unparsed", ())),
        effective_conditions=list(extras.get("effective", ())),
    )


def _school_alias_groups(services) -> dict[str, tuple[str, ...]]:
    """标准名/别名快照，供查询概念解析复用（与文档侧同一来源）。"""
    return SchoolReference(services.session_factory).alias_groups()


def _resolve_school_query(services, keyword: str) -> str:
    """把学校查询词解析到标准名；别名（如「北大」）统一到标准名（「北京大学」）。"""
    if not keyword:
        return keyword
    resolved = SchoolReference(services.session_factory).resolve(keyword)
    if resolved.get("matched"):
        return resolved.get("canonical_name") or keyword
    return keyword


def _like_escape(value: str) -> str:
    """转义 LIKE 通配符：反斜杠必须最先转义，否则会把后面补的转义符再转一次。

    用户输入里的 `%` / `_` 是普通字符，不转义就会被 SQL LIKE 当成通配符——
    例如输入 `%` 会变成「匹配所有人」，完全违背精确筛选的意图。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_structural_candidates(services, filters) -> set[str] | None:
    """手机号/性别/沟通文本在数据库层解析出匹配候选人集合，供索引缩小范围。"""
    with services.session_factory() as session:
        ids: set[str] | None = None
        if filters.phone:
            normalized = normalize_phone(filters.phone)
            if not normalized:
                return set()
            ids = set(session.scalars(
                select(CandidateContact.candidate_id)
                .join(Candidate, Candidate.id == CandidateContact.candidate_id)
                .where(CandidateContact.phone_fingerprint == normalized,
                       Candidate.deleted_at.is_(None))
            ).all())
        if filters.gender:
            target = normalize_gender(filters.gender)
            gender_ids: set[str] = set()
            rows = session.execute(
                select(ResumeDocument.candidate_id, ResumeRevision.parsed_data)
                .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
                .where(ResumeRevision.is_current.is_(True),
                       ResumeRevision.status == "READY",
                       Candidate.deleted_at.is_(None))
            ).all()
            for candidate_id, parsed in rows:
                if normalize_gender((parsed or {}).get("gender")) == target:
                    gender_ids.add(candidate_id)
            ids = gender_ids if ids is None else (ids & gender_ids)
        # 沟通文本：子串匹配（大小写不敏感）。空白输入视为没有该条件，避免退化成
        # 「匹配所有备注非空的人」；多个条件之间仍是交集，语义与 phone/gender 一致。
        note_keyword = (filters.communication_note or "").strip()
        if note_keyword:
            note_ids = set(session.scalars(
                select(Candidate.id)
                .where(Candidate.deleted_at.is_(None),
                       Candidate.communication_note.ilike(
                           f"%{_like_escape(note_keyword)}%", escape="\\"))
            ).all())
            ids = note_ids if ids is None else (ids & note_ids)
    return ids


def _hydrate_hits(services, hits, query, filters):
    """Batch join current business facts; a projection is never proof of eligibility."""
    items = []
    degraded = []
    seen_sha: set[str] = set()
    seen_candidates: set[str] = set()
    seen_identity: set[tuple[str, str]] = set()
    with services.session_factory() as session:
        pending = set(session.scalars(select(IndexSyncRecord.entity_id).where(
            IndexSyncRecord.entity_type == "candidate",
            IndexSyncRecord.entity_id.in_({hit.candidate_id for hit in hits}),
            IndexSyncRecord.requested_version > IndexSyncRecord.applied_version)).all())
        if pending:
            degraded.append("INDEX_SYNC_PENDING")
        rows = session.execute(
            select(Candidate, ResumeRevision, CandidateContact)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .outerjoin(CandidateContact, CandidateContact.candidate_id == Candidate.id)
            .where(Candidate.id.in_({hit.candidate_id for hit in hits}),
                   ResumeRevision.id.in_({hit.revision_id for hit in hits}),
                   Candidate.deleted_at.is_(None), Candidate.id.not_in(pending), ResumeRevision.is_current.is_(True),
                   ResumeRevision.status == "READY")
        ).all()
        current = {(candidate.id, revision.id): (candidate, revision, contact)
                   for candidate, revision, contact in rows}
        excluded: set[str] = set()
        if filters.exclude_skills:
            evidence_rows = session.execute(
                select(ResumeDocument.candidate_id, ResumeRevision)
                .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                .where(ResumeDocument.candidate_id.in_({candidate.id for candidate, _, _ in rows}),
                       ResumeRevision.is_current.is_(True))
            ).all()
            for candidate_id, revision in evidence_rows:
                if revision.status != "READY" or not revision.raw_text:
                    excluded.add(candidate_id)
                    degraded.append("EXCLUSION_UNVERIFIED")
                elif any(has_skill(revision.raw_text + "\n" + json.dumps(revision.parsed_data or {}, ensure_ascii=False), skill)
                         for skill in filters.exclude_skills):
                    excluded.add(candidate_id)
        for hit in hits:
            record = current.get((hit.candidate_id, hit.revision_id))
            if record is None or hit.candidate_id in seen_candidates or hit.candidate_id in excluded:
                continue
            candidate, revision, contact = record
            if filters.candidate_status and candidate.status != filters.candidate_status:
                continue
            identity_keys = [
                ("phone", contact.phone_fingerprint) if contact and contact.phone_fingerprint else None,
                ("email", contact.email_fingerprint) if contact and contact.email_fingerprint else None,
            ]
            identity_keys = [key for key in identity_keys if key]
            if any(key in seen_identity for key in identity_keys):
                continue
            seen_identity.update(identity_keys)
            sha = revision.content_sha256
            if sha and sha in seen_sha:
                continue
            if sha:
                seen_sha.add(sha)
            seen_candidates.add(candidate.id)
            encryption = services.encryption_service
            phone = (encryption.decrypt(contact.phone_encrypted)
                     if encryption and contact and contact.phone_encrypted else None)
            if filters.phone:
                normalized = normalize_phone(filters.phone)
                if not normalized or not _phone_matches(phone, contact, normalized):
                    continue
            if filters.gender:
                parsed_gender = normalize_gender((revision.parsed_data or {}).get("gender"))
                if parsed_gender != normalize_gender(filters.gender):
                    continue
            # 沟通文本：索引投影不是事实来源，`candidate_ids` 只缩小了范围，最终仍要按当前
            # SQLite 里的备注复核（与 phone/gender 同理）。空白输入等价于没有该条件。
            note_keyword = (filters.communication_note or "").strip()
            if note_keyword and not _field_contains(candidate.communication_note, note_keyword):
                continue
            parsed = revision.parsed_data or {}
            if filters.name and not _field_contains(candidate.display_name, filters.name):
                continue
            # 当前公司/职位作为有效检索值，与全部工作经历一起校验，人工值不因 experiences 缺失而被过滤。
            if filters.company and not (
                _field_contains(parsed.get("current_company"), filters.company)
                or _any_field_contains(parsed.get("experiences") or [], "company", filters.company)
            ):
                continue
            if filters.title and not (
                _field_contains(parsed.get("current_title"), filters.title)
                or _any_field_contains(parsed.get("experiences") or [], "title", filters.title)
            ):
                continue
            if filters.school and not _school_contains(parsed, filters.school):
                continue
            items.append(CandidateSearchItem(
                candidate_id=candidate.id, revision_id=revision.id, name=candidate.display_name,
                phone=phone, reasons=_build_reasons(query, hit),
                # 展示视图：历史遗留的碎片分点在读取时按整体段落重算（不写库）。
                parsed_data=repaired_profile_view(revision.parsed_data),
                content=hit.content, score=hit.score, matched_channels=hit.matched_channels,
                total_years=hit.total_years, highest_degree=hit.highest_degree, location=hit.location,
                qs_rank=hit.qs_rank, original_filename=revision.original_filename,
                communication_note=candidate.communication_note))
    return items, degraded


def _phone_matches(phone: str | None, contact, normalized: str) -> bool:
    """手机号精确匹配：规范化后必须完全一致。"""
    if contact is not None and contact.phone_fingerprint and normalized == contact.phone_fingerprint:
        return True
    if phone and normalized == normalize_phone(phone):
        return True
    return False


def _field_contains(value: str | None, keyword: str) -> bool:
    """字段内关键词匹配：大小写不敏感的子串命中。"""
    if not value or not keyword:
        return False
    return keyword.casefold() in str(value).casefold()


def _any_field_contains(items: list, field: str, keyword: str) -> bool:
    for item in items:
        if isinstance(item, dict) and _field_contains(item.get(field), keyword):
            return True
    return False


def _school_contains(parsed: dict, keyword: str) -> bool:
    if _field_contains(parsed.get("school"), keyword):
        return True
    for edu in parsed.get("educations") or []:
        if isinstance(edu, dict) and _field_contains(edu.get("school"), keyword):
            return True
    return False


def _build_reasons(query: str, hit) -> list[str]:
    """命中理由：通道、**已选证据**里的关键词命中（含片段原始 kind）、背景事实。

    命中依据只从 `hit.evidence` 生成，不再对 `hit.content` 做字符串包含判断 ——
    `content` 固定是 parent 概况，查询词只出现在 child 片段时也必须能给出依据。
    """
    reasons: list[str] = []
    if hit.matched_channels:
        reasons.append("匹配通道：" + "、".join(hit.matched_channels))
    terms = _evidence_terms(query, hit)
    if terms:
        reasons.append("关键词命中：" + "、".join(terms[:3]))
    facts = []
    if hit.total_years is not None:
        facts.append(f"{hit.total_years:g}年经验")
    if hit.highest_degree:
        facts.append(hit.highest_degree)
    if hit.location:
        facts.append(hit.location)
    if facts:
        reasons.append("背景：" + "、".join(facts))
    return reasons[:3]


def _evidence_terms(query: str, hit) -> list[str]:
    """查询词在已选证据里首次命中的位置：返回 ``词（原始 kind）``，按查询顺序去重。"""
    evidence = tuple(getattr(hit, "evidence", ()) or ())
    matched: list[str] = []
    for term in dict.fromkeys(t for t in query.split() if t):
        chunk = next((item for item in evidence if term in item.text), None)
        if chunk is not None:
            matched.append(f"{term}（{chunk.kind}）")
    return matched


def _merge_filters(parsed: CandidateFilters, explicit: CandidateFiltersRequest | CandidateFilters) -> CandidateFilters:
    """Supplied form values win, including false, null and empty collections."""
    merged = asdict(parsed)
    if isinstance(explicit, CandidateFiltersRequest):
        overrides = explicit.model_dump(exclude_unset=True)
    else:
        defaults = asdict(CandidateFilters())
        overrides = {key: value for key, value in asdict(explicit).items() if value != defaults[key]}
    for single, multiple in (("location", "locations"), ("preferred_location", "preferred_locations")):
        if single in overrides or multiple in overrides:
            merged[single], merged[multiple] = None, ()
    if "highest_degree" in overrides and overrides["highest_degree"]:
        overrides["highest_degree"] = normalize_degree(overrides["highest_degree"])
    if "highest_degree" in overrides and "degree_exact" not in overrides:
        merged["degree_exact"] = False
    merged.update(overrides)
    for key in ("locations", "preferred_locations", "exclude_skills", "specializations",
                "career_directions", "career_specializations", "business_directions", "companies"):
        merged[key] = tuple(merged[key] or ())
    return CandidateFilters(**merged)


class SearchReviewRequest(BaseModel):
    """搜索侧 AI 复核的输入：这次搜索用了什么条件、要复核哪些候选人。"""

    query: str = Field(default="", max_length=2_000)
    filters: CandidateFiltersRequest = Field(default_factory=CandidateFiltersRequest)
    candidate_ids: list[str] = Field(min_length=1, max_length=500)
    reasoning: bool = False


class SearchReviewStartResponse(BaseModel):
    review_id: str
    status: str
    query_key: str


class SearchReviewItem(BaseModel):
    candidate_id: str
    verdict: str
    highlights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    failed: bool = False
    error: str | None = None


class SearchReviewStatusResponse(BaseModel):
    status: str
    progress: int = 0
    error_message: str | None = None
    query_key: str | None = None
    items: list[SearchReviewItem] = Field(default_factory=list)


_SEARCH_REVIEW_TERMINAL_STATUSES = ("SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED")


def _latest_search_review_task(session, base_key: str) -> TaskRecord | None:
    """取该条件+该批候选人最新一次复核任务（重开时幂等键带 ``:{旧任务 id}`` 后缀）。"""
    return session.scalar(
        select(TaskRecord)
        .where(TaskRecord.idempotency_key.like(f"{base_key}%"))
        .order_by(TaskRecord.created_at.desc(), TaskRecord.id.desc())
        .limit(1)
    )


@router.post("/review", response_model=SearchReviewStartResponse)
def start_search_review(command: SearchReviewRequest, request: Request) -> SearchReviewStartResponse:
    """对搜索结果里勾选的候选人跑 AI 复核，产出「亮点 / 风险点」。

    重复点击语义与匹配复核一致：进行中 → 同一个任务；失败/死信 → 原地重排；
    SUCCESS/CANCELLED → 换新幂等键重新入队（否则 ``enqueue`` 只会把旧任务还回来）。
    """
    services: AppServices = request.app.state.services
    if services.search_review_service is None:
        raise ApiError(503, "E_SEARCH_REVIEW_UNAVAILABLE", "AI 复核服务未配置")
    filters = command.filters.model_dump()
    query_key = query_fingerprint(command.query, filters)
    conditions = describe_conditions(command.query, filters)
    base_key = review_base_key(query_key, command.candidate_ids)
    with services.session_factory() as session:
        latest = _latest_search_review_task(session, base_key)
        latest_id, latest_status = (latest.id, latest.status) if latest is not None else (None, None)
    if latest_id is not None and latest_status not in _SEARCH_REVIEW_TERMINAL_STATUSES:
        return SearchReviewStartResponse(
            review_id=latest_id, status=latest_status or "QUEUED", query_key=query_key
        )
    if latest_id is not None and latest_status in ("FAILED", "DEAD_LETTER"):
        services.task_repository.retry(latest_id)
        return SearchReviewStartResponse(review_id=latest_id, status="QUEUED", query_key=query_key)
    key = base_key if latest_id is None else f"{base_key}:{latest_id}"
    task_id = services.task_repository.enqueue(TaskSpec(
        task_type="SEARCH_REVIEW",
        queue_name="batch",
        priority=5,
        payload={
            "query_key": query_key,
            "conditions": conditions,
            "candidate_ids": list(command.candidate_ids),
            "reasoning": command.reasoning,
        },
        idempotency_key=key,
    ))
    return SearchReviewStartResponse(review_id=task_id, status="QUEUED", query_key=query_key)


@router.get("/review/{review_id}", response_model=SearchReviewStatusResponse)
def get_search_review(review_id: str, request: Request) -> SearchReviewStatusResponse:
    """查询复核进度与**已落库**的结论：结论逐条写入，长任务也能边跑边看。"""
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        task = session.get(TaskRecord, review_id)
        if task is None:
            raise ApiError(404, "E_TASK_NOT_FOUND", "复核任务不存在")
        payload = task.payload or {}
        query_key = payload.get("query_key")
        candidate_ids = list(payload.get("candidate_ids") or [])
        rows = [] if not (query_key and candidate_ids) else list(session.scalars(
            select(SearchReview)
            .where(SearchReview.query_key == query_key,
                   SearchReview.candidate_id.in_(candidate_ids))
            .order_by(SearchReview.created_at, SearchReview.id)
        ))
        return SearchReviewStatusResponse(
            status=task.status,
            progress=task.progress,
            error_message=task.error_message,
            query_key=query_key,
            items=[
                SearchReviewItem(
                    candidate_id=row.candidate_id,
                    verdict=row.verdict,
                    highlights=list(row.highlights or []),
                    risks=list(row.risks or []),
                    failed=row.failed,
                    error=row.error,
                )
                for row in rows
            ],
        )
