import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.profile_stream import profile_generation_stream, wants_event_stream
from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import Jd, JdRevision
from kerui_recruit.direction.policy import (
    MAX_CAREER_DIRECTIONS_JD,
    apply_direction_normalization,
)
from kerui_recruit.jd.deletion import JdDeletionService
from kerui_recruit.jd.extract import UnsupportedJdType, extract_jd_text, split_jd_text
from kerui_recruit.jd.ingest import IngestJd, JdIngestService
from kerui_recruit.jd.profile_constraints import (
    CONSTRAINT_KINDS,
    CONSTRAINT_STRENGTHS,
    normalize_constraints,
    parse_years_requirement,
)
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.profile_pair import REGEN_TIMEOUT_SECONDS
from kerui_recruit.cases.state import refresh_links
from kerui_recruit.search.sync import enqueue_sync

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/jd", tags=["jd"])


class ImportJdRequest(BaseModel):
    company: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    source_text: str = Field(min_length=1, max_length=100_000)


class ImportJdResponse(BaseModel):
    jd_id: str
    revision_id: str


@router.post("/import", response_model=ImportJdResponse, status_code=202)
async def import_jd(command: ImportJdRequest, request: Request) -> ImportJdResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        result = JdIngestService(session).ingest(
            IngestJd(company=command.company, title=command.title, source_text=command.source_text)
        )
    return ImportJdResponse(jd_id=result.jd_id, revision_id=result.revision_id)


@router.post("/import-file", response_model=ImportJdResponse, status_code=202)
async def import_jd_file(
    request: Request,
    file: UploadFile = File(...),
    company: str = Form(""),
    title: str = Form(""),
) -> ImportJdResponse:
    services: AppServices = request.app.state.services
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise ApiError(413, "E_FILE_TOO_LARGE", "JD 文件不能超过 10MB")
    filename = file.filename or "jd"
    try:
        source_text = extract_jd_text(filename, content)
    except UnsupportedJdType:
        raise ApiError(415, "E_JD_FILE_TYPE_UNSUPPORTED", "仅支持 Word 和 Excel 格式的 JD 文件")
    if not source_text.strip():
        raise ApiError(422, "E_JD_EMPTY", "无法从 JD 文件中提取到文本")

    with services.session_factory() as session:
        result = JdIngestService(session).ingest(
            IngestJd(
                company=company.strip() or "未知",
                title=title.strip() or Path(filename).stem,
                source_text=source_text,
            )
        )
    return ImportJdResponse(jd_id=result.jd_id, revision_id=result.revision_id)


class ImportJdBatchRequest(BaseModel):
    source_text: str = Field(min_length=1, max_length=500_000)


class ImportJdBatchResponse(BaseModel):
    imported: list[ImportJdResponse]


@router.post("/import-batch", response_model=ImportJdBatchResponse, status_code=202)
async def import_jd_batch(
    command: ImportJdBatchRequest, request: Request
) -> ImportJdBatchResponse:
    """Import one or more JDs from a single text blob (split then AI parse)."""
    services: AppServices = request.app.state.services
    if services.jd_pipeline is not None:
        chunks = await services.jd_pipeline.split(command.source_text)
    else:
        chunks = split_jd_text(command.source_text)
    imported: list[ImportJdResponse] = []
    with services.session_factory() as session:
        service = JdIngestService(session)
        for index, chunk in enumerate(chunks):
            result = service.ingest(
                IngestJd(company="", title="", source_text=chunk, passive_match=(index == 0))
            )
            imported.append(
                ImportJdResponse(jd_id=result.jd_id, revision_id=result.revision_id)
            )
    return ImportJdBatchResponse(imported=imported)


@router.post("/import-batch-file", response_model=ImportJdBatchResponse, status_code=202)
async def import_jd_batch_file(
    request: Request, file: UploadFile = File(...)
) -> ImportJdBatchResponse:
    """Import one or more JDs from a Word/Excel file (split then AI parse)."""
    services: AppServices = request.app.state.services
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise ApiError(413, "E_FILE_TOO_LARGE", "JD 文件不能超过 10MB")
    filename = file.filename or "jd"
    try:
        source_text = extract_jd_text(filename, content)
    except UnsupportedJdType:
        raise ApiError(415, "E_JD_FILE_TYPE_UNSUPPORTED", "仅支持 Word 和 Excel 格式的 JD 文件")
    if not source_text.strip():
        raise ApiError(422, "E_JD_EMPTY", "无法从 JD 文件中提取到文本")

    if services.jd_pipeline is not None:
        chunks = await services.jd_pipeline.split(source_text)
    else:
        chunks = split_jd_text(source_text)
    imported: list[ImportJdResponse] = []
    with services.session_factory() as session:
        service = JdIngestService(session)
        for index, chunk in enumerate(chunks):
            result = service.ingest(
                IngestJd(company="", title="", source_text=chunk, passive_match=(index == 0))
            )
            imported.append(
                ImportJdResponse(jd_id=result.jd_id, revision_id=result.revision_id)
            )
    return ImportJdBatchResponse(imported=imported)


class JdListItem(BaseModel):
    jd_id: str
    revision_id: str
    company: str
    title: str
    status: str
    jd_status: str
    ai_category: str | None
    location: str | None
    min_years: float | None
    parsed_data: dict | None = None
    source_text: str | None = None


class JdPage(BaseModel):
    items: list[JdListItem]
    total: int
    page: int
    page_size: int
    has_more: bool


def _jd_filters(title: str | None, company: str | None, status: str | None):
    conditions = []
    if title:
        conditions.append(Jd.title.ilike(f"%{title}%"))
    if company:
        conditions.append(Jd.company.ilike(f"%{company}%"))
    if status:
        conditions.append(Jd.status == status)
    return conditions


def _jd_item(jd: Jd, revision: JdRevision) -> JdListItem:
    return JdListItem(
        jd_id=jd.id,
        revision_id=revision.id,
        company=jd.company,
        title=jd.title,
        status=revision.status,
        jd_status=jd.status,
        ai_category=revision.ai_category,
        location=revision.location,
        min_years=float(revision.min_years) if revision.min_years is not None else None,
        parsed_data=revision.parsed_data,
        source_text=revision.source_text,
    )


@router.get("", response_model=list[JdListItem])
def list_jds(request: Request) -> list[JdListItem]:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        rows = session.execute(
            select(Jd, JdRevision)
            .join(JdRevision, JdRevision.jd_id == Jd.id)
            .where(Jd.deleted_at.is_(None), JdRevision.is_current.is_(True))
            .order_by(Jd.created_at.desc())
        ).all()
    return [_jd_item(jd, revision) for jd, revision in rows]


@router.get("/page", response_model=JdPage)
def list_jds_page(
    request: Request,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    company: str | None = None,
    status: str | None = None,
) -> JdPage:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ApiError(422, "E_PAGE_INVALID", "页码必须大于0，每页不能超过200条")
    services: AppServices = request.app.state.services
    filters = _jd_filters(title, company, status)
    with services.session_factory() as session:
        total = int(session.scalar(
            select(func.count()).select_from(Jd).join(JdRevision, JdRevision.jd_id == Jd.id).where(
                Jd.deleted_at.is_(None), JdRevision.is_current.is_(True), *filters)) or 0)
        rows = session.execute(
            select(Jd, JdRevision)
            .join(JdRevision, JdRevision.jd_id == Jd.id)
            .where(Jd.deleted_at.is_(None), JdRevision.is_current.is_(True), *filters)
            .order_by(Jd.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    items = [_jd_item(jd, revision) for jd, revision in rows]
    return JdPage(items=items, total=total, page=page, page_size=page_size,
                  has_more=page * page_size < total)


class UpdateJdStatusRequest(BaseModel):
    status: str = Field(pattern="^(OPEN|FILLED|CANCELLED)$")


class JdStatusResponse(BaseModel):
    jd_id: str
    status: str


@router.patch("/{jd_id}/status", response_model=JdStatusResponse)
def update_jd_status(
    jd_id: str, command: UpdateJdStatusRequest, request: Request
) -> JdStatusResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session, session.begin():
        jd = session.get(Jd, jd_id)
        if jd is None:
            raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
        jd.status = command.status
        refresh_links(session, jd_id=jd_id)
        enqueue_sync(session, "jd", jd_id)
    return JdStatusResponse(jd_id=jd_id, status=command.status)


_AI_CATEGORY_ALIASES = {
    "AI": "CORE_AI",
    "AI相关": "AI_RELATED",
    "非AI": "NON_AI",
    "CORE_AI": "CORE_AI",
    "AI_RELATED": "AI_RELATED",
    "NON_AI": "NON_AI",
}


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split("、") if s.strip()]
    if isinstance(value, (list, tuple)):
        return [str(s).strip() for s in value if str(s).strip()]
    return [str(value).strip()]


# 解析表可编辑的 JD 字段（ParsedJd 全字段）。
_JD_EDITABLE_FIELDS = frozenset({
    "title", "company", "department", "location", "salary", "ai_category",
    "industry", "min_years", "highest_degree", "qs_level", "core_duties",
    "required_skills", "plus_skills", "plus_industry", "plus_project_types",
    "summary", "candidate_profile", "requirements", "direction", "exact_constraints",
    # 多值方向体系（职业大类 ≤3 / 职业细分 / 业务方向 ≤2）；
    # career_taxonomy_version 由后端盖章，不开放编辑。
    "career_directions", "career_specializations", "business_directions",
})

_JD_STRING_LIST_FIELDS = frozenset({
    "core_duties", "required_skills", "plus_skills", "plus_industry", "plus_project_types",
    "career_directions", "career_specializations", "business_directions",
})

# 编辑这些字段时需要对多值方向做合法化/限流并刷新镜像字段。
_JD_DIRECTION_EDITABLE_FIELDS = frozenset({
    "direction", "career_directions", "career_specializations", "business_directions",
})

_EXACT_CONSTRAINT_KINDS = CONSTRAINT_KINDS
_EXACT_CONSTRAINT_STRENGTHS = CONSTRAINT_STRENGTHS


def _coerce_jd_field(field: str, value: Any) -> Any:
    if field in _JD_STRING_LIST_FIELDS:
        return _coerce_string_list(value)
    if field == "min_years":
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ApiError(422, "E_JD_FIELD_INVALID", "工作年限无效")
    if field == "ai_category":
        if value in (None, ""):
            return None
        alias = _AI_CATEGORY_ALIASES.get(str(value).strip())
        if alias is None:
            raise ApiError(422, "E_JD_AI_CATEGORY_INVALID", "AI 分类无效")
        return alias
    if field == "exact_constraints":
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ApiError(422, "E_JD_FIELD_INVALID", "硬条件格式不正确")
        result: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                raise ApiError(422, "E_JD_FIELD_INVALID", "硬条件格式不正确")
            kind = str(item.get("kind") or "").strip()
            strength = str(item.get("strength") or "").strip()
            if kind not in _EXACT_CONSTRAINT_KINDS or strength not in _EXACT_CONSTRAINT_STRENGTHS:
                raise ApiError(422, "E_JD_FIELD_INVALID", "硬条件 kind/strength 无效")
            alternatives = [str(a).strip() for a in (item.get("alternatives") or []) if str(a).strip()]
            if not alternatives:
                raise ApiError(422, "E_JD_FIELD_INVALID", "硬条件 alternatives 不能为空")
            result.append({
                "kind": kind,
                "operator": "OR" if str(item.get("operator") or "OR") == "OR" else "AND",
                "alternatives": alternatives,
                "strength": strength,
                "source": str(item.get("source") or "manual"),
                "source_text": str(item.get("source_text") or ""),
            })
        # 与存库读取侧同一套规整：skill / other_keyword 的 MUST 在这里就降级，
        # 避免界面显示「必备」而实际不具备淘汰力这种表里不一。
        return normalize_constraints(result)
    if field == "requirements":
        if not isinstance(value, list):
            raise ApiError(422, "E_JD_FIELD_INVALID", "岗位要求格式不正确")
        result: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                raise ApiError(422, "E_JD_FIELD_INVALID", "岗位要求格式不正确")
            kind = str(item.get("kind") or "").strip()
            if kind not in ("MUST", "PLUS", "EXCLUDE"):
                raise ApiError(422, "E_JD_FIELD_INVALID", "岗位要求 kind 无效")
            label = str(item.get("label") or "").strip()
            val = str(item.get("value") or "").strip()
            if not label or not val:
                raise ApiError(422, "E_JD_FIELD_INVALID", "岗位要求 label/value 不能为空")
            result.append({"kind": kind, "label": label, "value": val})
        return result
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        # title/company/summary 是必填字符串（非 Optional），空值必须保留为 ""，否则 model_validate 报错。
        if field in ("title", "company", "summary"):
            return stripped
        return stripped or None
    return value


def _apply_profile_years(parsed: dict, revision: JdRevision, *, explicit_min_years: bool = False) -> None:
    """画像文本里写了年限就以画像为准（最新的人工口径），没写则保留原值。

    年限是**独立于 exact_constraints** 的硬窗口（``[n-1, 2n]``，见
    ``match/service._years_window``），原本只能来自 JD 原文解析或「解析字段」编辑器——
    在画像里写「5 年以上」此前不会产生任何筛选。``years`` 为 ``None`` 表示画像明确
    「经验不限」，要清掉年限要求；``stated=False`` 时**不动** ``min_years``，
    避免每次改画像都把 JD 解析出的年限抹掉。

    ``explicit_min_years=True``（解析表编辑器一次提交全字段）时直接让路：用户当场手填的
    年限是比画像文本更明确的意图。
    """
    if explicit_min_years:
        return
    stated, years = parse_years_requirement(parsed.get("candidate_profile"))
    if not stated:
        return
    value = None if years is None else (int(years) if float(years).is_integer() else float(years))
    parsed["min_years"] = value
    revision.min_years = value


class UpdateJdFieldRequest(BaseModel):
    field: str
    value: Any = None
    # 双形态画像：与 value（整体段落）同源的分点与浓缩，由「重新生成」结果原样透传；
    # 纯手工编辑时为空，后端回退到按句读确定性拆点。
    points: list[dict] | None = None
    compact: str | None = None


class UpdateJdFieldResponse(BaseModel):
    jd_id: str
    revision_id: str
    field: str
    value: Any


@router.put("/{jd_id}/field", response_model=UpdateJdFieldResponse)
def update_jd_field(
    jd_id: str, command: UpdateJdFieldRequest, request: Request
) -> UpdateJdFieldResponse:
    """Update an editable JD field (title/company/ai_category) in place."""
    services: AppServices = request.app.state.services
    field = command.field
    with services.session_factory() as session, session.begin():
        jd = session.get(Jd, jd_id)
        if jd is None:
            raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
        revision = session.scalar(
            select(JdRevision).where(
                JdRevision.jd_id == jd_id, JdRevision.is_current.is_(True)
            )
        )
        if revision is None:
            raise ApiError(404, "E_JD_REVISION_NOT_FOUND", "岗位版本不存在")

        parsed = dict(revision.parsed_data or {})
        if field == "title":
            value = str(command.value or "").strip()
            jd.title = value
            parsed["title"] = value
        elif field == "company":
            value = str(command.value or "").strip()
            jd.company = value
            parsed["company"] = value
        elif field == "ai_category":
            value = _AI_CATEGORY_ALIASES.get(str(command.value or "").strip())
            if value is None:
                raise ApiError(422, "E_JD_AI_CATEGORY_INVALID", "AI 分类无效")
            revision.ai_category = value
            parsed["ai_category"] = value
        elif field == "candidate_profile":
            value = str(command.value or "").strip() or None
            parsed["candidate_profile"] = value
            from kerui_recruit.providers.profile_pair import build_profile_pair, normalize_points, split_profile_clauses
            points = normalize_points(command.points)
            parsed["candidate_profile_narrative"] = value
            if points:
                # 重新生成链路已产出真双形态：原样落库，不再按标点伪拆点。
                parsed["candidate_profile_points"] = points
                compact = (command.compact or "").strip()
                parsed["candidate_profile_compact"] = compact or points[0]["text"][:60]
            else:
                # 人工编辑：按句读切分，不调用模型、不增删事实。
                parsed["candidate_profile_points"] = [
                    {"text": p, "evidence_paths": []}
                    for p in split_profile_clauses(value or "")
                ]
                parsed["candidate_profile_compact"] = build_profile_pair(value or "").compact or None
            overrides = dict(revision.manual_overrides or {})
            overrides["candidate_profile"] = value
            revision.manual_overrides = overrides
            _apply_profile_years(parsed, revision)
        elif field == "exact_constraints":
            value = _coerce_jd_field("exact_constraints", command.value)
            parsed["exact_constraints"] = value
            overrides = dict(revision.manual_overrides or {})
            overrides["exact_constraints"] = value
            revision.manual_overrides = overrides
        elif field == "department":
            value = str(command.value or "").strip() or None
            parsed["department"] = value
        elif field == "location":
            value = str(command.value or "").strip() or None
            revision.location = value
            parsed["location"] = value
        elif field == "min_years":
            raw = command.value
            if raw in (None, ""):
                revision.min_years = None
                parsed["min_years"] = None
                value = None
            else:
                try:
                    years = int(raw)
                except (TypeError, ValueError):
                    raise ApiError(422, "E_JD_FIELD_INVALID", "工作年限无效")
                revision.min_years = years
                parsed["min_years"] = years
                value = years
        else:
            raise ApiError(422, "E_JD_FIELD_UNSUPPORTED", "不支持的字段")
        revision.parsed_data = parsed
        enqueue_sync(session, "jd", jd_id)

    return UpdateJdFieldResponse(
        jd_id=jd_id, revision_id=revision.id, field=field, value=value
    )


class JdParsedUpdateRequest(BaseModel):
    parsed_data: dict


@router.put("/{jd_id}/parsed", response_model=UpdateJdFieldResponse)
def update_jd_parsed(
    jd_id: str, command: JdParsedUpdateRequest, request: Request
) -> UpdateJdFieldResponse:
    """批量保存 JD 解析数据（解析表）：一次提交全字段，索引异步重建。"""
    services: AppServices = request.app.state.services
    incoming = command.parsed_data or {}
    updates = {k: v for k, v in incoming.items() if k in _JD_EDITABLE_FIELDS}
    if not updates:
        raise ApiError(422, "E_JD_FIELD_UNSUPPORTED", "没有可编辑的岗位字段")

    with services.session_factory() as session, session.begin():
        jd = session.get(Jd, jd_id)
        if jd is None:
            raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
        revision = session.scalar(
            select(JdRevision).where(
                JdRevision.jd_id == jd_id, JdRevision.is_current.is_(True)
            )
        )
        if revision is None:
            raise ApiError(404, "E_JD_REVISION_NOT_FOUND", "岗位版本不存在")

        parsed = dict(revision.parsed_data or {})
        for field, value in updates.items():
            parsed[field] = _coerce_jd_field(field, value)

        # 画像双形态：重新生成链路透传真分点/浓缩时原样落库，否则按句读确定性拆点。
        if "candidate_profile" in updates:
            from kerui_recruit.providers.profile_pair import build_profile_pair, normalize_points, split_profile_clauses
            profile = parsed.get("candidate_profile")
            points = normalize_points(incoming.get("candidate_profile_points"))
            parsed["candidate_profile_narrative"] = profile
            if points:
                parsed["candidate_profile_points"] = points
                compact = str(incoming.get("candidate_profile_compact") or "").strip()
                parsed["candidate_profile_compact"] = compact or points[0]["text"][:60]
            else:
                parsed["candidate_profile_points"] = [
                    {"text": p, "evidence_paths": []}
                    for p in split_profile_clauses(profile or "")
                ]
                parsed["candidate_profile_compact"] = build_profile_pair(profile or "").compact or None
            # 画像里写了年限就以画像为准（画像属于最新人工口径）；但同一次提交里
            # 手填过 min_years 时让路给显式字段。
            _apply_profile_years(parsed, revision, explicit_min_years="min_years" in updates)

        # 多值方向：合法化 + 限流（JD 上限 3 个大类），并刷新单值 direction 与评估镜像。
        if _JD_DIRECTION_EDITABLE_FIELDS & updates.keys():
            apply_direction_normalization(parsed, max_directions=MAX_CAREER_DIRECTIONS_JD)

        # 人工画像与硬条件标记为 manual override，避免后续重新解析覆盖（与单字段接口一致）。
        overrides = dict(revision.manual_overrides or {})
        if "candidate_profile" in updates:
            overrides["candidate_profile"] = parsed.get("candidate_profile")
        if "exact_constraints" in updates:
            overrides["exact_constraints"] = parsed.get("exact_constraints")
        if overrides != (revision.manual_overrides or {}):
            revision.manual_overrides = overrides

        # 同步派生列，保证列表页展示与硬过滤使用最新值。
        if "title" in updates:
            jd.title = parsed.get("title") or ""
        if "company" in updates:
            jd.company = parsed.get("company") or ""
        if "ai_category" in updates:
            revision.ai_category = parsed.get("ai_category")
        if "min_years" in updates:
            revision.min_years = parsed.get("min_years")
        if "location" in updates:
            revision.location = parsed.get("location")

        try:
            ParsedJd.model_validate(parsed)
        except ValidationError as error:
            raise ApiError(422, "E_JD_FIELD_INVALID", "岗位字段格式不正确") from error

        revision.parsed_data = parsed
        # 异步重建索引：只入队，不阻塞保存。
        enqueue_sync(session, "jd", jd_id)

    return UpdateJdFieldResponse(
        jd_id=jd_id, revision_id=revision.id, field="parsed_data", value=parsed
    )


class RegenJdProfileRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class ParseConstraintsRequest(BaseModel):
    source_text: str = Field(default="", max_length=10_000)


class ParseConstraintsResponse(BaseModel):
    constraints: list[dict]
    # 年限独立于 exact_constraints（它是 min_years 驱动的硬窗口）；
    # years_stated 区分「画像没提年限」（保留原值）与「画像明确不限」（清空窗口）。
    min_years: float | None = None
    years_stated: bool = False


@router.post("/parse-constraints", response_model=ParseConstraintsResponse)
async def parse_constraints(command: ParseConstraintsRequest, request: Request) -> ParseConstraintsResponse:
    """按给定画像文本重解析硬条件与年限（模型 + 规则，**并集**）。

    两路都要跑，结果并起来（见 ``merge_requirements``）：模型擅长语义判断（哪句是硬条件、
    强度怎么定），规则擅长格式固定的客观条件（学历 / 学校档次 / 点名公司 / 年限）。实测模型会把
    「本科及以上学历，5 年以上经验，具备阿里或字节背景」整段判成空数组，只跑模型会把这些明写的
    条件静默丢掉，所以不能「模型非空就取代规则」。年限同理：模型优先，模型没给出结论时走规则。
    """
    from dataclasses import asdict
    from kerui_recruit.jd.profile_constraints import merge_requirements, parse_exact_constraints

    rule_constraints = normalize_constraints([
        asdict(c) for c in parse_exact_constraints(command.source_text, source="manual")
    ])

    services: AppServices = request.app.state.services
    generator = getattr(services.backfill_service, "jd_generator", None)
    requirements = None
    if generator is not None:
        try:
            requirements = await generator.parse_constraints(command.source_text)
        except Exception:  # noqa: BLE001 - 降级路径必须留痕，不能把保存流程卡死
            logger.exception("AI 要求解析失败，退化为确定性规则")

    constraints = merge_requirements(
        requirements.constraints if requirements is not None else [], rule_constraints
    )
    if requirements is not None and requirements.years_stated:
        stated, years = True, requirements.min_years
    else:
        stated, years = parse_years_requirement(command.source_text)
    return ParseConstraintsResponse(
        constraints=constraints, min_years=years if stated else None, years_stated=stated
    )


def _jd_profile_error(error: BaseException) -> ApiError:
    """生成期异常 → ApiError。**同步与 SSE 两条路径共用这一份**，否则同一个失败会给出不同的码。"""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, asyncio.TimeoutError):
        return ApiError(
            504, "E_PROFILE_TIMEOUT",
            f"画像生成超过 {REGEN_TIMEOUT_SECONDS:.0f} 秒未返回，已放弃本次生成；原画像保留，请稍后重试。",
        )
    if isinstance(error, LookupError):
        return ApiError(404, "E_JD_NOT_FOUND", str(error))
    if isinstance(error, ProviderError):
        # 供应商不可用（E_AI_NO_PROVIDER / 限流 / 鉴权）不是内部错误：映射成 502/503，
        # 前端才能区分「可重试的服务不可用」与「真出错了」。实测原来一律压成 500。
        return ApiError(error.http_status, error.code, error.user_message, error.details)
    return ApiError(500, "E_PROFILE_GENERATION_FAILED", f"画像生成失败：{error}")


async def _generate_jd_profile(
    services: AppServices, jd_id: str, instruction: str | None, on_stage=None
) -> dict:
    """生成一次 JD 画像（不落库）。同步与 SSE 两条路径的唯一实现。"""
    if services.backfill_service is None:
        raise ApiError(500, "E_PROFILE_UNAVAILABLE", "画像生成服务不可用")
    # 服务端硬超时：模型抖动时给出明确文案，而不是让界面一直转圈（问题 #6 的一半）。
    # 取 `REGEN_TIMEOUT_SECONDS`（略大于画像重写预算），超时后重写预算也就没有意义了。
    return await asyncio.wait_for(
        services.backfill_service.regenerate_jd_profile(
            jd_id, instruction=instruction, on_stage=on_stage
        ),
        timeout=REGEN_TIMEOUT_SECONDS,
    )


@router.post("/{jd_id}/regen-profile")
async def regenerate_jd_profile(jd_id: str, command: RegenJdProfileRequest, request: Request):
    """重新生成 JD 候选人画像要求；生成失败保留旧画像并返回明确错误。

    `Accept: text/event-stream` 时改为 SSE，先推阶段（`loading`/`draft`/`repair`）再推结果——
    交互式入口最坏等 150 秒，只有转圈看不出「还会不会再来一次模型调用」。详见
    `api/profile_stream.py`（含「为什么复用同一路径做内容协商」）。
    """
    services: AppServices = request.app.state.services
    if wants_event_stream(request):
        return profile_generation_stream(
            lambda on_stage: _generate_jd_profile(
                services, jd_id, command.instruction or None, on_stage),
            translate=_jd_profile_error,
            timeout_seconds=REGEN_TIMEOUT_SECONDS,
        )
    try:
        return await _generate_jd_profile(services, jd_id, command.instruction or None)
    except Exception as error:
        raise _jd_profile_error(error) from error


@router.delete("/{jd_id}")
def delete_jd(jd_id: str, request: Request) -> dict:
    """物理删除岗位：永久清除 JD 及其版本、要求、匹配结果与索引。"""
    services: AppServices = request.app.state.services
    jd_index = services.index_sync_service.jd_index if services.index_sync_service else None
    deletion = JdDeletionService(services.session_factory, jd_index=jd_index)
    if not deletion.delete(jd_id):
        raise ApiError(404, "E_JD_NOT_FOUND", "岗位不存在")
    return {"jd_id": jd_id, "deleted": True}


class BulkDeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class BulkItemResponse(BaseModel):
    entity_id: str
    ok: bool
    error: str | None = None
    extra: dict = Field(default_factory=dict)


class BulkDeleteResponse(BaseModel):
    results: list[BulkItemResponse]
    succeeded: int
    failed: int


@router.post("/bulk/delete", response_model=BulkDeleteResponse)
def bulk_delete_jds_endpoint(command: BulkDeleteRequest, request: Request) -> BulkDeleteResponse:
    """批量物理删除岗位：逐项返回结果，extra 携带**保留**的流程数量（岗位信息转快照）。"""
    from kerui_recruit.bulk.service import bulk_delete_jds
    services: AppServices = request.app.state.services
    result = bulk_delete_jds(services, command.ids)
    return BulkDeleteResponse(
        results=[BulkItemResponse(entity_id=r.entity_id, ok=r.ok, error=r.error, extra=r.extra) for r in result.results],
        succeeded=result.succeeded,
        failed=result.failed,
    )
