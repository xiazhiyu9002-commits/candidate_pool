import asyncio
import re
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.profile_stream import profile_generation_stream, wants_event_stream
from kerui_recruit.api.services import AppServices
from kerui_recruit.bulk.service import (
    bulk_delete_candidates,
    bulk_download_candidates,
    bulk_force_ocr,
    bulk_reparse,
)
from kerui_recruit.db.base import new_id
from kerui_recruit.db.models import (
    Candidate,
    CandidateContact,
    ResumeDocument,
    ResumeRevision,
    TaskRecord,
)
from kerui_recruit.direction.policy import (
    apply_direction_normalization,
    extract_multi_directions,
    is_pending_career,
)
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.profile_pair import REGEN_TIMEOUT_SECONDS, repaired_profile_view
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.resumes.deletion import CandidateDeletionService
from kerui_recruit.resumes.extract import LegacyDocConversionError, convert_doc_to_pdf
from kerui_recruit.resumes.normalize import derive_education_compat, normalize_resume
from kerui_recruit.duplicates.service import normalize_email, normalize_phone
from kerui_recruit.resumes.structured import ParsedResume
from kerui_recruit.resumes.validity import check_parsed_resume
from kerui_recruit.resumes.work_years_rollover import established_age_baseline
from kerui_recruit.schools.reference import recompute_educations
from kerui_recruit.search.sync import enqueue_sync
from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.tasks.repository import TaskSpec, reparse_idempotency_key


router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class ImportResumeResponse(BaseModel):
    action: str
    candidate_id: str | None
    document_id: str | None
    revision_id: str | None
    blob_id: str | None
    task_id: str | None
    message: str = ""
    conflict_candidate_ids: list[str] = Field(default_factory=list)
    created_task: bool = False


class ImportFolderRequest(BaseModel):
    directory: str


class ImportFolderResponse(BaseModel):
    imported: list[ImportResumeResponse]
    skipped: list[str]
    errors: list[str]


@router.post("/import", response_model=ImportResumeResponse, status_code=202)
async def import_resume(request: Request, file: UploadFile = File(...)) -> ImportResumeResponse:
    services: AppServices = request.app.state.services
    content = await file.read(30 * 1024 * 1024 + 1)
    if len(content) > 30 * 1024 * 1024:
        raise ApiError(413, "E_FILE_TOO_LARGE", "单个简历文件不能超过 30MB")
    filename = file.filename or "resume"
    with services.session_factory() as session:
        result = ResumeIngestService(session, services.blob_store).ingest(
            IngestResume(filename=filename, content=content, queue_name="interactive")
        )
    return ImportResumeResponse(**asdict(result))


@router.post("/import-folder", response_model=ImportFolderResponse)
def import_folder(command: ImportFolderRequest, request: Request) -> ImportFolderResponse:
    services: AppServices = request.app.state.services
    directory = Path(command.directory).expanduser()
    if not directory.is_dir():
        raise ApiError(400, "E_INVALID_DIRECTORY", "目录不存在或不可访问")

    imported: list[ImportResumeResponse] = []
    skipped: list[str] = []
    errors: list[str] = []

    files: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ResumeIngestService.allowed_suffixes:
            skipped.append(path.name)
            continue
        files.append(path)

    # 批量入库时只让第一个候选人触发被动匹配，其余按需通过"匹配"按钮处理。
    for index, path in enumerate(files):
        try:
            content = path.read_bytes()
            if len(content) > 30 * 1024 * 1024:
                errors.append(f"{path.name}: 文件超过 30MB")
                continue
            with services.session_factory() as session:
                result = ResumeIngestService(session, services.blob_store).ingest(
                    IngestResume(
                        filename=path.name,
                        content=content,
                        queue_name="normal",
                        passive_match=(index == 0),
                    )
                )
            if result.action == "ALREADY_IMPORTED":
                skipped.append(path.name)
            elif result.action == "DUPLICATE_CONFLICT":
                errors.append(f"{path.name}: 相同文件已关联到多个候选人")
            else:
                imported.append(ImportResumeResponse(**asdict(result)))
        except Exception as exc:  # noqa: BLE001 - report per-file failures
            errors.append(f"{path.name}: {exc}")
    return ImportFolderResponse(imported=imported, skipped=skipped, errors=errors)


class CandidateListItem(BaseModel):
    candidate_id: str
    revision_id: str
    display_name: str
    total_years: float | None
    highest_degree: str | None
    location: str | None
    status: str
    revision_status: str | None
    phone: str | None
    original_filename: str | None
    parsed_data: dict | None
    error_code: str | None = None
    error_message: str | None = None
    # 沟通记录：候选人级自由文本，**不在 parsed_data 里**，也不参与画像与索引。
    communication_note: str | None = None


class CandidatePage(BaseModel):
    items: list[CandidateListItem]
    total: int
    page: int
    page_size: int
    has_more: bool


def _candidate_rows(services: AppServices, *, offset: int | None = None, limit: int | None = None):
    latest_revision = (
        select(ResumeRevision.id)
        .join(ResumeDocument, ResumeRevision.document_id == ResumeDocument.id)
        .where(ResumeDocument.candidate_id == Candidate.id, ResumeRevision.is_current.is_(True))
        .order_by(ResumeRevision.created_at.desc(), ResumeRevision.id.desc())
        .limit(1).correlate(Candidate).scalar_subquery()
    )
    stmt = (select(Candidate, ResumeRevision, CandidateContact).select_from(Candidate)
            .join(ResumeRevision, ResumeRevision.id == latest_revision)
            .outerjoin(CandidateContact, CandidateContact.candidate_id == Candidate.id)
            .where(Candidate.deleted_at.is_(None))
            .order_by(Candidate.created_at.desc(), Candidate.id.desc()))
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    with services.session_factory() as session:
        return session.execute(stmt).all()


def _candidate_items(services: AppServices, rows) -> list[CandidateListItem]:
    encryption = services.encryption_service
    return [CandidateListItem(
        candidate_id=candidate.id, revision_id=revision.id, display_name=candidate.display_name,
        total_years=float(candidate.total_years) if candidate.total_years is not None else None,
        highest_degree=candidate.highest_degree, location=(revision.parsed_data or {}).get("location"),
        status=candidate.status, revision_status=revision.status,
        phone=(encryption.decrypt(contact.phone_encrypted)
               if encryption is not None and contact is not None and contact.phone_encrypted else None),
        original_filename=revision.original_filename,
        # 展示视图：历史遗留的碎片分点在读取时按整体段落重算（不写库）。
        parsed_data=repaired_profile_view(revision.parsed_data),
        error_code=revision.error_code, error_message=revision.error_message,
        communication_note=candidate.communication_note,
    ) for candidate, revision, contact in rows]


@router.get("/candidates", response_model=list[CandidateListItem])
def list_candidates(request: Request) -> list[CandidateListItem]:
    services: AppServices = request.app.state.services
    return _candidate_items(services, _candidate_rows(services))


@router.get("/candidates/page", response_model=CandidatePage)
def list_candidate_page(request: Request, page: int = 1, page_size: int = 100) -> CandidatePage:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ApiError(422, "E_PAGE_INVALID", "页码必须大于0，每页不能超过200条")
    services: AppServices = request.app.state.services
    has_current = select(ResumeRevision.id).join(ResumeDocument).where(
        ResumeDocument.candidate_id == Candidate.id, ResumeRevision.is_current.is_(True)).exists()
    with services.session_factory() as session:
        total = int(session.scalar(select(func.count()).select_from(Candidate).where(
            Candidate.deleted_at.is_(None), has_current)) or 0)
    rows = _candidate_rows(services, offset=(page - 1) * page_size, limit=page_size)
    return CandidatePage(items=_candidate_items(services, rows), total=total, page=page,
                         page_size=page_size, has_more=page * page_size < total)


@router.get("/candidates/direction-pending", response_model=list[CandidateListItem])
def list_direction_pending(request: Request) -> list[CandidateListItem]:
    """方向待核队列：当前 READY revision 的职业方向缺失或仅 OTHER/非法的候选人。"""
    services: AppServices = request.app.state.services
    pending = [
        (candidate, revision, contact)
        for candidate, revision, contact in _candidate_rows(services)
        if revision.status == "READY"
        and is_pending_career(extract_multi_directions(revision.parsed_data or {})[0])
    ]
    return _candidate_items(services, pending)


class ResumeRevisionItem(BaseModel):
    revision_id: str
    display_name: str | None
    original_filename: str
    status: str
    is_current: bool
    created_at: datetime
    parsed_data: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


@router.get("/candidate/{candidate_id}/revisions", response_model=list[ResumeRevisionItem])
def list_revisions(candidate_id: str, request: Request) -> list[ResumeRevisionItem]:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revisions = session.scalars(
            select(ResumeRevision)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(ResumeDocument.candidate_id == candidate_id)
            .order_by(ResumeRevision.created_at.desc())
        ).all()
    return [
        ResumeRevisionItem(
            revision_id=r.id,
            display_name=r.display_name,
            original_filename=r.original_filename,
            status=r.status,
            is_current=r.is_current,
            created_at=r.created_at,
            parsed_data=r.parsed_data,
            error_code=r.error_code,
            error_message=r.error_message,
        )
        for r in revisions
    ]


@router.post("/revisions/{revision_id}/switch", response_model=ResumeRevisionItem)
def switch_revision(revision_id: str, request: Request) -> ResumeRevisionItem:
    services: AppServices = request.app.state.services
    with services.session_factory() as session, session.begin():
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        session.execute(
            update(ResumeRevision)
            .where(ResumeRevision.document_id == revision.document_id)
            .values(is_current=False)
        )
        revision.is_current = True
        candidate = revision.document.candidate
        if revision.status == "READY":
            for field in ("name", "total_years", "highest_degree"):
                _sync_candidate_columns(candidate, field, (revision.parsed_data or {}).get(field))
        enqueue_sync(session, "candidate", candidate.id)
        session.flush()
        return ResumeRevisionItem(
            revision_id=revision.id,
            display_name=revision.display_name,
            original_filename=revision.original_filename,
            status=revision.status,
            is_current=revision.is_current,
            created_at=revision.created_at,
            parsed_data=revision.parsed_data,
        )


class ReparseResumeRequest(BaseModel):
    force_ocr: bool = False
    use_vision: bool = True


class ReparseResumeResponse(BaseModel):
    revision_id: str
    task_id: str


@router.post(
    "/revisions/{revision_id}/reparse",
    response_model=ReparseResumeResponse,
    status_code=202,
)
def reparse_resume(
    revision_id: str,
    command: ReparseResumeRequest,
    request: Request,
) -> ReparseResumeResponse:
    """对既有简历版本创建一次强制 OCR 重新解析任务（幂等）。

    复用已有原件与候选人身份，不要求重新上传；同一版本重复点击返回同一任务，
    不会被文件去重逻辑拦截。
    """
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        force_ocr = bool(command.force_ocr)
        use_vision = bool(command.use_vision)
        mode = "vision" if use_vision else "ocr"
        key = reparse_idempotency_key(session, revision_id, mode)
        task_id = services.task_repository.enqueue(
            TaskSpec(
                task_type="PARSE_RESUME",
                queue_name="interactive",
                priority=10,
                payload={"revision_id": revision_id, "force_ocr": force_ocr, "use_vision": use_vision},
                idempotency_key=key,
            )
        )
    # 同名任务已失败终态时重新入队，支持失败后再次点击重试；进行中的任务保持幂等。
    with services.session_factory() as session:
        existing = session.get(TaskRecord, task_id)
        needs_retry = existing is not None and existing.status in ("FAILED", "DEAD_LETTER")
    if needs_retry:
        services.task_repository.retry(task_id)
    return ReparseResumeResponse(revision_id=revision_id, task_id=task_id)


def _review_response(revision: ResumeRevision) -> dict:
    return {
        "revision_id": revision.id, "status": revision.status,
        "candidate_id": revision.document.candidate_id,
        "review_required": revision.status != "READY",
        "raw_text": revision.raw_text,
        "parsed_data": revision.parsed_data, "review_data": revision.review_data,
        "manual_overrides": revision.manual_overrides or {},
        "extraction_diagnostics": revision.extraction_diagnostics or {},
        "error_code": revision.error_code, "error_message": revision.error_message,
    }


@router.get("/revisions/{revision_id}/review")
def get_resume_review(revision_id: str, request: Request) -> dict:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        return _review_response(revision)


@router.get("/revisions/{revision_id}/download")
def download_resume(revision_id: str, request: Request) -> FileResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        path = services.blob_store.root / revision.blob.storage_path
        filename = revision.original_filename
    if not path.exists():
        raise ApiError(404, "E_BLOB_NOT_FOUND", "原始文件不存在")
    import mimetypes
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type)


@router.get("/revisions/{revision_id}/view-target")
def resume_view_target(revision_id: str, request: Request) -> dict:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        source = services.blob_store.root / revision.blob.storage_path
        filename = revision.original_filename
    if not source.is_file():
        raise ApiError(404, "E_BLOB_NOT_FOUND", "原始文件不存在")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".doc", ".docx"):
        return {"kind": "preview", "filename": filename}
    # A fresh, separate copy prevents edits in Word from changing the blob or
    # another already-open document. Prefixing also avoids Windows device names.
    name = re.split(r"[/\\]", filename)[-1]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).stem).strip(" .")[:80]
    directory = services.settings.paths.temp / "open-documents"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=f"resume-{stem}-", suffix=suffix, delete=False) as target:
            target_path = Path(target.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, target)
    except OSError as error:
        raise ApiError(500, "E_DOCUMENT_OPEN", "无法创建简历查看副本") from error
    return {"kind": "word", "filename": filename, "path": str(target_path.resolve())}


@router.get("/revisions/{revision_id}/preview")
def preview_resume(revision_id: str, request: Request) -> FileResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        path = services.blob_store.root / revision.blob.storage_path
        filename = revision.original_filename
    if not path.exists():
        raise ApiError(404, "E_BLOB_NOT_FOUND", "原始文件不存在")

    suffix = Path(filename).suffix.lower()
    if suffix in (".doc", ".docx"):
        try:
            pdf_path = convert_doc_to_pdf(path)
        except LegacyDocConversionError as error:
            raise ApiError(422, "E_PREVIEW_UNSUPPORTED", str(error))
        return FileResponse(
            pdf_path,
            filename=Path(filename).stem + ".pdf",
            media_type="application/pdf",
            content_disposition_type="inline",
        )

    import mimetypes
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        content_disposition_type="inline",
    )


class CandidateContactResponse(BaseModel):
    email: str | None
    phone: str | None
    email_confidence: float | None
    phone_confidence: float | None


class CandidateContactUpdate(BaseModel):
    email: str | None = None
    phone: str | None = None


def _decrypt_contact(
    contact: CandidateContact | None,
    encryption: EncryptionService | None,
) -> CandidateContactResponse:
    email = (
        encryption.decrypt(contact.email_encrypted)
        if contact is not None and contact.email_encrypted and encryption is not None
        else None
    )
    phone = (
        encryption.decrypt(contact.phone_encrypted)
        if contact is not None and contact.phone_encrypted and encryption is not None
        else None
    )
    return CandidateContactResponse(
        email=email,
        phone=phone,
        email_confidence=contact.email_confidence if contact is not None else None,
        phone_confidence=contact.phone_confidence if contact is not None else None,
    )


@router.get("/candidate/{candidate_id}/contact", response_model=CandidateContactResponse)
def get_candidate_contact(candidate_id: str, request: Request) -> CandidateContactResponse:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        contact = session.scalar(
            select(CandidateContact).where(CandidateContact.candidate_id == candidate_id)
        )
    return _decrypt_contact(contact, services.encryption_service)


@router.put("/candidate/{candidate_id}/contact", response_model=CandidateContactResponse)
def update_candidate_contact(
    candidate_id: str,
    command: CandidateContactUpdate,
    request: Request,
) -> CandidateContactResponse:
    services: AppServices = request.app.state.services
    encryption = services.encryption_service
    if encryption is None:
        raise ApiError(500, "E_ENCRYPTION_UNAVAILABLE", "加密服务不可用")
    with services.session_factory() as session, session.begin():
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
        contact = session.scalar(
            select(CandidateContact).where(CandidateContact.candidate_id == candidate_id)
        )
        if contact is None:
            contact = CandidateContact(candidate=candidate)
            session.add(contact)
        contact.email_encrypted = encryption.encrypt(command.email) if command.email else None
        contact.phone_encrypted = encryption.encrypt(command.phone) if command.phone else None
        contact.email_confidence = 1.0 if command.email else None
        contact.phone_confidence = 1.0 if command.phone else None
        contact.email_fingerprint = normalize_email(command.email)
        contact.phone_fingerprint = normalize_phone(command.phone)
        contact.manual_fields = ["email", "phone"]
        session.flush()
        return CandidateContactResponse(
            email=command.email,
            phone=command.phone,
            email_confidence=contact.email_confidence,
            phone_confidence=contact.phone_confidence,
        )


# 字段修改对索引的影响分类：
# - 影响向量内容（需重新 embedding + 重建索引）
# - 仅影响索引过滤元数据（无需重建向量，只更新索引里的对应列）
# - 仅影响数据库（不改索引）
_VECTOR_CONTENT_FIELDS = frozenset({
    "name", "summary", "ai_profile_summary", "highest_degree", "total_years", "school",
    "qs_rank", "graduation_year", "industry", "skills",
    "experiences", "projects", "current_industry", "longest_industry",
})
_METADATA_ONLY_FIELDS = frozenset({
    "location", "school_level", "preferred_location", "preferred_locations",
    # 多值方向是索引过滤列，不进入向量文本；编辑后只需刷新索引元数据。
    "career_directions", "career_specializations", "business_directions", "specializations",
})
_CONTACT_FIELDS = frozenset({"email", "phone"})
_LIST_FIELDS = frozenset({
    "skills", "preferred_locations",
    "career_directions", "career_specializations", "business_directions", "specializations",
})
_INT_FIELDS = frozenset({"qs_rank", "graduation_year", "birth_year", "age"})
# 参与 AI 画像生成的输入字段；编辑这些字段后，AI 生成的画像应标记为 stale。
# 与画像生成侧（resumes/profile.py）共用同一份规范，避免过期判定与生成输入口径漂移。
_PROFILE_INPUT_FIELDS = frozenset(profile_spec.CANDIDATE_PROFILE_INPUT_FIELDS)

# 解析表可编辑的候选人字段（排除画像与系统内部字段，画像走独立编辑器）。
_CANDIDATE_EDITABLE_FIELDS = frozenset({
    "name", "total_years", "highest_degree", "location", "preferred_location",
    "preferred_locations", "school", "school_level", "qs_rank", "school_tier",
    "graduation_year", "birth_year", "age", "gender", "salary", "job_level",
    "industry", "current_industry", "longest_industry", "skills", "summary",
    "experiences", "projects", "educations", "current_company", "current_title",
    "direction",
    # 多值方向体系（职业大类 / 职业细分 / 业务方向）。
    # career_taxonomy_version 由后端盖章，不开放编辑。
    "career_directions", "career_specializations", "business_directions",
})

# 编辑这些字段时需要对多值方向做合法化/限流并刷新镜像字段。
_DIRECTION_EDITABLE_FIELDS = frozenset({
    "direction", "career_directions", "career_specializations", "business_directions",
})


def _coerce_field(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _LIST_FIELDS:
        if isinstance(value, str):
            return [s.strip() for s in re.split(r"[、,，/]", value) if s.strip()]
        if isinstance(value, list):
            return [s.strip() for s in value if isinstance(s, str) and s.strip()]
        return value
    if field in _INT_FIELDS:
        if isinstance(value, str) and not value.strip():
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field == "total_years":
        if isinstance(value, str) and not value.strip():
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _sync_candidate_columns(candidate: Candidate, field: str, value: Any) -> None:
    if field == "name":
        if value:
            candidate.display_name = value
    elif field == "total_years":
        candidate.total_years = (
            Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if value is not None
            else None
        )
    elif field == "highest_degree":
        candidate.highest_degree = value


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _reindex_embedded(services: AppServices, candidate_id: str, revision_id: str) -> None:
    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        if revision is None:
            return
        parsed = revision.parsed_data or {}
        display_name = revision.document.candidate.display_name if revision.document.candidate else None
    from kerui_recruit.search.documents import build_candidate_document
    document = build_candidate_document(parsed, display_name=display_name)
    vectors = await services.search_service.embedding_provider.embed_documents([document["vector_text"]])
    chunk = SearchChunk(
        id=new_id(),
        candidate_id=candidate_id,
        revision_id=revision_id,
        content=document["keyword_text"],
        vector=tuple(vectors[0]),
        total_years=_to_float(parsed.get("total_years")),
        highest_degree=parsed.get("highest_degree"),
        location=parsed.get("location"),
        candidate_status="AVAILABLE",
        qs_rank=parsed.get("qs_rank"),
        school_level=parsed.get("school_level"),
        preferred_location=parsed.get("preferred_location"),
        preferred_locations=tuple(parsed.get("preferred_locations") or []),
        keyword_text=document["keyword_text"],
        vector_text=document["vector_text"],
        name_terms=tuple(document["name_terms"]),
        school_terms=tuple(document["school_terms"]),
        company_terms=tuple(document["company_terms"]),
        title_terms=tuple(document["title_terms"]),
        location_terms=tuple(document["location_terms"]),
        skills=tuple(document["skills"]),
        age=document["age"],
        school_tags=tuple(document["school_tags"]),
    )
    index = services.search_service.index
    await asyncio.to_thread(index.delete_revision, revision_id)
    await asyncio.to_thread(index.upsert, [chunk])


def _reindex_metadata(services: AppServices, revision_id: str, field: str, value: Any) -> None:
    index = services.search_service.index
    rows = index.get_revision_chunks(revision_id)
    if not rows:
        return
    updated = []
    for row in rows:
        data = dict(row)
        data[field] = value
        updated.append(
            SearchChunk(
                id=data["id"],
                candidate_id=data["candidate_id"],
                revision_id=data["revision_id"],
                content=data.get("keyword_text") or "",
                vector=tuple(float(x) for x in data["vector"]),
                total_years=data.get("total_years"),
                highest_degree=data.get("highest_degree"),
                location=data.get("location"),
                candidate_status=data.get("candidate_status", "AVAILABLE"),
                qs_rank=data.get("qs_rank"),
                school_level=data.get("school_level"),
                preferred_location=data.get("preferred_location"),
                preferred_locations=tuple(data.get("preferred_locations") or []),
                keyword_text=data.get("keyword_text"),
                vector_text=data.get("vector_text"),
                name_terms=tuple(data.get("name_terms") or []),
                school_terms=tuple(data.get("school_terms") or []),
                company_terms=tuple(data.get("company_terms") or []),
                title_terms=tuple(data.get("title_terms") or []),
                location_terms=tuple(data.get("location_terms") or []),
                skills=tuple(data.get("skills") or []),
                age=data.get("age"),
                school_tags=tuple(data.get("school_tags") or []),
                direction=data.get("direction"),
                specializations=tuple(data.get("specializations") or []),
                career_directions=tuple(data.get("career_directions") or []),
                career_specializations=tuple(data.get("career_specializations") or []),
                business_directions=tuple(data.get("business_directions") or []),
            )
        )
    index.delete_revision(revision_id)
    index.upsert(updated)


class CandidateCommunicationNoteUpdate(BaseModel):
    """沟通记录：单条自由文本，整条覆盖式保存（空字符串表示清空）。"""

    note: str = Field(default="", max_length=2000)


@router.put("/candidate/{candidate_id}/communication-note")
def update_candidate_communication_note(
    candidate_id: str, command: CandidateCommunicationNoteUpdate, request: Request
) -> dict:
    """保存候选人的沟通记录。

    刻意与 `PUT /candidate/{id}/field` 分开：后者编辑的是 `parsed_data`，会触发
    画像过期判定（`ai_profile_stale`）与索引重建入队。沟通记录是**候选人级备注**，
    放进 parsed_data 会顺着画像污染向量，所以这里只写 `candidate.communication_note`，
    不碰 revision、不碰画像状态、不入队索引。
    """
    services: AppServices = request.app.state.services
    note = command.note.strip()
    with services.session_factory() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
        candidate.communication_note = note or None
        session.commit()
    return {"candidate_id": candidate_id, "communication_note": note or None}


class CandidateFieldUpdate(BaseModel):
    field: str
    value: Any = None
    # 双形态画像：与 value（整体段落）同源的分点与浓缩，由「重新生成」结果原样透传；
    # 纯手工编辑时为空，后端回退到按句读确定性拆点。
    points: list[dict] | None = None
    compact: str | None = None


@router.put("/candidate/{candidate_id}/field")
async def update_candidate_field(
    candidate_id: str,
    command: CandidateFieldUpdate,
    request: Request,
) -> dict:
    services: AppServices = request.app.state.services
    field = command.field
    value = command.value

    if field in _CONTACT_FIELDS:
        encryption = services.encryption_service
        if encryption is None:
            raise ApiError(500, "E_ENCRYPTION_UNAVAILABLE", "加密服务不可用")
        with services.session_factory() as session, session.begin():
            candidate = session.get(Candidate, candidate_id)
            if candidate is None:
                raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
            contact = session.scalar(
                select(CandidateContact).where(CandidateContact.candidate_id == candidate_id)
            )
            if contact is None:
                contact = CandidateContact(candidate=candidate)
                session.add(contact)
            if field == "phone":
                contact.phone_encrypted = encryption.encrypt(value) if value else None
                contact.phone_confidence = 1.0 if value else None
                contact.phone_fingerprint = normalize_phone(value)
            else:
                contact.email_encrypted = encryption.encrypt(value) if value else None
                contact.email_confidence = 1.0 if value else None
                contact.email_fingerprint = normalize_email(value)
            contact.manual_fields = sorted(set(contact.manual_fields or []) | {field})
            session.flush()
        return {"candidate_id": candidate_id, "field": field, "value": value}

    if field not in ParsedResume.model_fields and field != "age":
        raise ApiError(422, "E_INVALID_RESUME_FIELD", "不支持的简历字段")

    with services.session_factory() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
        revision = session.scalar(
            select(ResumeRevision)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(
                ResumeDocument.candidate_id == candidate_id,
                ResumeRevision.is_current.is_(True),
            )
        )
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")
        revision_id = revision.id
        parsed = dict(revision.parsed_data or {})
        parsed[field] = _coerce_field(field, value)
        if field == "educations":
            # 编辑教育经历后按学校名重新计算派生属性，再重算兼容字段，保证展示与筛选一致。
            educations = recompute_educations(services.session_factory, parsed.get("educations") or [])
            parsed["educations"] = educations
            parsed.update(derive_education_compat(educations))
        if field == "ai_profile_summary":
            # 人工编辑画像：标记为 manual、清空输入哈希，且不再视为过期。
            parsed["ai_profile_source"] = "manual"
            parsed["ai_profile_input_hash"] = None
            parsed["ai_profile_stale"] = False
            summary = parsed["ai_profile_summary"]
            from kerui_recruit.providers.profile_pair import build_profile_pair, normalize_points, split_profile_clauses
            points = normalize_points(command.points)
            parsed["ai_profile_narrative"] = summary
            if points:
                # 重新生成链路已产出真双形态：原样落库，不再按标点伪拆点。
                parsed["ai_profile_points"] = points
                compact = (command.compact or "").strip()
                parsed["ai_profile_compact"] = compact or points[0]["text"][:60]
            else:
                # 纯手工编辑：按句读切分，不调用模型、不增删事实。
                parsed["ai_profile_points"] = [
                    {"text": p, "evidence_paths": []}
                    for p in split_profile_clauses(summary or "")
                ]
                parsed["ai_profile_compact"] = build_profile_pair(summary or "").compact or None
        elif field in _PROFILE_INPUT_FIELDS and parsed.get("ai_profile_source") == "ai":
            # 参与画像生成的输入字段变化后，AI 画像标记为过期，待重新生成。
            parsed["ai_profile_stale"] = True
        if field in _DIRECTION_EDITABLE_FIELDS:
            # 多值方向：合法化 + 限流，并刷新单值 direction 与 direction_assessment 镜像。
            apply_direction_normalization(parsed)
        try:
            ParsedResume.model_validate(parsed)
        except ValidationError as error:
            raise ApiError(422, "E_INVALID_RESUME_FIELD", "简历字段格式不正确") from error
        revision.parsed_data = parsed
        revision.manual_overrides = {**(revision.manual_overrides or {}), field: parsed[field]}
        _sync_candidate_columns(candidate, field, parsed[field])
        ready = revision.status == "READY"
        enqueue_sync(session, "candidate", candidate_id)
        session.commit()

    if ready and services.index_sync_service is not None:
        # 同步等待该候选人的索引同步完成，消除「保存后立即查询」的短暂漏人窗口。
        await services.index_sync_service.run_once(
            entity_type="candidate", entity_id=candidate_id, force=True)
    elif ready and services.index_sync_service is None and field in _VECTOR_CONTENT_FIELDS:
        await _reindex_embedded(services, candidate_id, revision_id)
    elif ready and services.index_sync_service is None and field in _METADATA_ONLY_FIELDS:
        _reindex_metadata(services, revision_id, field, parsed[field])

    return {
        "candidate_id": candidate_id,
        "revision_id": revision_id,
        "field": field,
        "value": parsed[field],
    }


class CandidateParsedUpdateRequest(BaseModel):
    parsed_data: dict


@router.put("/candidate/{candidate_id}/parsed")
def update_candidate_parsed(
    candidate_id: str, command: CandidateParsedUpdateRequest, request: Request
) -> dict:
    """批量保存候选人解析数据（解析表）：一次提交可编辑字段，索引异步重建。"""
    services: AppServices = request.app.state.services
    incoming = command.parsed_data or {}
    updates = {k: v for k, v in incoming.items() if k in _CANDIDATE_EDITABLE_FIELDS}
    if not updates:
        raise ApiError(422, "E_INVALID_RESUME_FIELD", "没有可编辑的简历字段")

    with services.session_factory() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
        revision = session.scalar(
            select(ResumeRevision)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(
                ResumeDocument.candidate_id == candidate_id,
                ResumeRevision.is_current.is_(True),
            )
        )
        if revision is None:
            raise ApiError(404, "E_REVISION_NOT_FOUND", "简历版本不存在")

        parsed = dict(revision.parsed_data or {})
        for field, value in updates.items():
            parsed[field] = _coerce_field(field, value)

        if "educations" in updates:
            educations = recompute_educations(services.session_factory, parsed.get("educations") or [])
            parsed["educations"] = educations
            parsed.update(derive_education_compat(educations))

        # 多值方向：合法化 + 限流，并刷新单值 direction 与 direction_assessment 镜像。
        if _DIRECTION_EDITABLE_FIELDS & updates.keys():
            apply_direction_normalization(parsed)

        # 画像输入字段变化 → 标记 AI 画像过期（ai_profile_summary 不在可编辑范围）。
        if _PROFILE_INPUT_FIELDS & updates.keys() and parsed.get("ai_profile_source") == "ai":
            parsed["ai_profile_stale"] = True

        # 人工改过年龄后仍要逐年增长：把基准重置为「人工值 + 当年」，否则下一次日滚动
        # 会按旧基准把人工值覆盖回去。
        if "age" in updates and parsed.get("age") is not None:
            parsed.update(established_age_baseline(int(parsed["age"]), datetime.now().year))

        try:
            ParsedResume.model_validate(parsed)
        except ValidationError as error:
            raise ApiError(422, "E_INVALID_RESUME_FIELD", "简历字段格式不正确") from error

        revision.parsed_data = parsed
        revision.manual_overrides = {**(revision.manual_overrides or {}), **updates}
        for field in ("name", "total_years", "highest_degree"):
            if field in updates:
                _sync_candidate_columns(candidate, field, parsed.get(field))
        # 异步重建索引：只入队，不阻塞保存，由后台 index_sync_service 消费。
        enqueue_sync(session, "candidate", candidate_id)
        session.commit()

    return {"candidate_id": candidate_id, "revision_id": revision.id, "updated_fields": sorted(updates)}


class RegenCandidateProfileRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)


def _candidate_profile_error(error: BaseException) -> ApiError:
    """生成期异常 → ApiError。**同步与 SSE 两条路径共用这一份**，否则同一个失败会给出不同的码。"""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, asyncio.TimeoutError):
        return ApiError(
            504, "E_PROFILE_TIMEOUT",
            f"画像生成超过 {REGEN_TIMEOUT_SECONDS:.0f} 秒未返回，已放弃本次生成；原画像保留，请稍后重试。",
        )
    if isinstance(error, LookupError):
        return ApiError(404, "E_CANDIDATE_NOT_FOUND", str(error))
    if isinstance(error, ProviderError):
        # 供应商不可用（E_AI_NO_PROVIDER / 限流 / 鉴权）不是内部错误：映射成 502/503，
        # 前端才能区分「可重试的服务不可用」与「真出错了」。实测原来一律压成 500。
        return ApiError(error.http_status, error.code, error.user_message, error.details)
    return ApiError(500, "E_PROFILE_GENERATION_FAILED", f"画像生成失败：{error}")


async def _generate_candidate_profile(
    services: AppServices, candidate_id: str, instruction: str | None, on_stage=None
) -> dict:
    """生成一次候选人画像（不落库）。同步与 SSE 两条路径的唯一实现。"""
    if services.backfill_service is None:
        raise ApiError(500, "E_PROFILE_UNAVAILABLE", "画像生成服务不可用")
    # 服务端硬超时：模型抖动时给出明确文案，而不是让界面一直转圈（问题 #6 的一半）。
    # 取 `REGEN_TIMEOUT_SECONDS`（略大于画像重写预算），超时后重写预算也就没有意义了。
    return await asyncio.wait_for(
        services.backfill_service.regenerate_candidate_profile(
            candidate_id, instruction=instruction, on_stage=on_stage
        ),
        timeout=REGEN_TIMEOUT_SECONDS,
    )


@router.post("/candidate/{candidate_id}/regen-profile")
async def regenerate_candidate_profile(
    candidate_id: str, command: RegenCandidateProfileRequest, request: Request
):
    """重新生成候选人 AI 画像；生成失败保留旧画像并返回明确错误。

    `Accept: text/event-stream` 时改为 SSE，先推阶段（`loading`/`draft`/`repair`）再推结果。
    详见 `api/profile_stream.py`。
    """
    services: AppServices = request.app.state.services
    if wants_event_stream(request):
        return profile_generation_stream(
            lambda on_stage: _generate_candidate_profile(
                services, candidate_id, command.instruction or None, on_stage),
            translate=_candidate_profile_error,
            timeout_seconds=REGEN_TIMEOUT_SECONDS,
        )
    try:
        return await _generate_candidate_profile(
            services, candidate_id, command.instruction or None)
    except Exception as error:
        raise _candidate_profile_error(error) from error


@router.delete("/candidate/{candidate_id}")
def delete_candidate(candidate_id: str, request: Request) -> dict:
    """物理删除候选人：保留流程历史（快照），永久清除简历与候选人数据。"""
    services: AppServices = request.app.state.services
    deletion = CandidateDeletionService(
        services.session_factory,
        services.search_service.index,
        blob_store=services.blob_store,
        task_repository=services.task_repository,
    )
    deleted = deletion.delete(candidate_id)
    if not deleted:
        raise ApiError(404, "E_CANDIDATE_NOT_FOUND", "候选人不存在")
    return {"candidate_id": candidate_id, "deleted": True}


class BulkRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class BulkItemResponse(BaseModel):
    entity_id: str
    ok: bool
    error: str | None = None
    extra: dict = Field(default_factory=dict)


class BulkResponse(BaseModel):
    results: list[BulkItemResponse]
    succeeded: int
    failed: int


def _bulk_response(result) -> BulkResponse:
    return BulkResponse(
        results=[BulkItemResponse(entity_id=r.entity_id, ok=r.ok, error=r.error, extra=r.extra) for r in result.results],
        succeeded=result.succeeded,
        failed=result.failed,
    )


@router.post("/bulk/delete", response_model=BulkResponse)
def bulk_delete_candidates_endpoint(command: BulkRequest, request: Request) -> BulkResponse:
    """批量物理删除候选人：逐项返回成功/失败；失败项保持选中供重试。"""
    services: AppServices = request.app.state.services
    return _bulk_response(bulk_delete_candidates(services, command.ids))


@router.post("/bulk/reparse", response_model=BulkResponse)
def bulk_reparse_endpoint(command: BulkRequest, request: Request) -> BulkResponse:
    """批量重新解析（force_ocr=false）：解析结果不理想时的常规重试入口。

    与「强制 OCR」的区别：只有异常页走 OCR，正常页仍走文本/视觉解析。
    """
    services: AppServices = request.app.state.services
    return _bulk_response(bulk_reparse(services, command.ids))


@router.post("/bulk/force-ocr", response_model=BulkResponse)
def bulk_force_ocr_endpoint(command: BulkRequest, request: Request) -> BulkResponse:
    """批量强制 OCR：按选中版本入队真正的 OCR 解析任务（force_ocr=true, use_vision=false）。"""
    services: AppServices = request.app.state.services
    return _bulk_response(bulk_force_ocr(services, command.ids))


@router.post("/bulk/download")
def bulk_download_endpoint(command: BulkRequest, request: Request) -> Response:
    """批量下载：选中候选人的最新有效原件打包为 ZIP（文件缺失列在结果摘要中）。"""
    services: AppServices = request.app.state.services
    content, result = bulk_download_candidates(services, command.ids)
    summary = ",".join(
        f"{r.entity_id}:{'ok' if r.ok else (r.error or 'failed')}" for r in result.results
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="resumes_batch.zip"',
            # HTTP 头的值只能装 latin-1，而这段摘要会带上中文失败原因（「无有效原件」
            # 「原始文件缺失」）。直接放原文会让**整批下载**以 500 收场，而不是「部分成功」——
            # 2026-09-22 全量接口探针实测就挂在这里：
            #     'latin-1' codec can't encode characters in position 37（= uuid 之后的那个冒号）
            # 按 RFC 3986 百分号编码后再放进头里；前端只把响应体当 ZIP，契约不变。
            "X-Bulk-Result": quote(summary[:2000], safe=",=:;"),
        },
    )
