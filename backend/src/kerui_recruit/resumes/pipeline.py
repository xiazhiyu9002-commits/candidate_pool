from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.base import new_id
from kerui_recruit.db.models import Candidate, CandidateContact, ResumeDocument, ResumeImportClaim, ResumeRevision
from kerui_recruit.duplicates.service import normalize_email, normalize_phone
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.contracts import EmbeddingProvider, OCRProvider
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.vision_parse import VisionStructuredParser
from kerui_recruit.resumes.extract import (
    UNKNOWN_NAME,
    ExtractedText,
    extract_contact,
    extract_name,
    extract_text,
    name_from_filename,
)
from kerui_recruit.resumes.identity import resolve_identity
from kerui_recruit.resumes.normalize import normalize_resume
from kerui_recruit.resumes.revision_purge import (
    inherit_manual_overrides,
    purge_superseded_revisions,
)
from kerui_recruit.resumes.quality import (
    DOMINANT_FRAGMENT_RATIO,
    analyze_text,
)
from kerui_recruit.resumes.structured import NormalizedResume, ParsedResume, ResumeParser
from kerui_recruit.resumes.validity import check_parsed_resume
from kerui_recruit.search.contracts import SearchChunk, SearchIndex
from kerui_recruit.storage.blobs import BlobStore

logger = logging.getLogger(__name__)


def _ai_parse_route(parser: object) -> str:
    """本次解析实际走的是远程 AI 还是本地确定性解析。

    为什么必须记下来：`_RoutedResumeParser` 在**没有可用快速路由**（`E_AI_NO_PROVIDER`）时
    会回退 `LocalResumeParser`。这条兜底本身是设计（AI 智能解析默认关闭的用户要走它），
    但**静默**就变成误导——2026-09-22 真机验收里，智谱/火山两轮因为连接没有 FAST_TEXT 角色
    全部走了本地解析，任务 0.2~3.6 秒就「成功」，界面显示解析完成而画像为空，
    看起来像「供应商能解析但解析质量差」，实际一次模型调用都没发生。

    不认识的实现按「远程」处理：失败开放，不凭空制造降级结论。
    """
    probe = getattr(parser, "uses_remote_ai", None)
    if probe is None:
        return "remote"
    try:
        return "remote" if probe() else "local"
    except Exception:
        logger.warning("判断解析路线失败，按远程解析记录", exc_info=True)
        return "remote"


_DEGREE_SHORT = {
    "博士": "博",
    "硕士": "硕",
    "本科": "本",
    "大专": "专",
    "PhD": "博",
    "Master": "硕",
    "Bachelor": "本",
}


class PipelineFailure(RuntimeError):
    """解析流水线的可分类失败；``code`` 会被任务与修订版本记录。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def build_display_name(resume: NormalizedResume, suffix: str) -> str:
    """Generate a human-readable resume display name from parsed fields."""
    parts: list[str] = [resume.name or UNKNOWN_NAME]
    if resume.total_years is not None:
        parts.append(f"{int(resume.total_years)}年")
    degree = resume.highest_degree or ""
    if degree:
        parts.append(_DEGREE_SHORT.get(degree, degree))
    skills = list(resume.skills)[:2]
    if skills:
        parts.append("+".join(skills))
    return "-".join(parts) + suffix


@dataclass(frozen=True, slots=True)
class PipelineResult:
    candidate_id: str
    revision_id: str
    status: str
    chunks: tuple[SearchChunk, ...]


class ResumePipeline:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        parser: ResumeParser,
        embedding_provider: EmbeddingProvider,
        ocr_provider: OCRProvider | None = None,
        vision_parser: VisionStructuredParser | None = None,
        search_index: SearchIndex | None = None,
        defer_indexing: bool = False,
        encryption_service: EncryptionService | None = None,
        task_repository=None,
    ) -> None:
        self.session_factory = session_factory
        self.blob_store = blob_store
        self.parser = parser
        self.embedding_provider = embedding_provider
        self.ocr_provider = ocr_provider
        self.vision_parser = vision_parser
        self.search_index = search_index
        self.defer_indexing = defer_indexing
        self.encryption_service = encryption_service
        # 版本级硬删除需要登记可重试的物理文件清理任务；缺省时退化为立即删除。
        self.task_repository = task_repository

    async def run(self, revision_id: str, *, force_ocr: bool = False, use_vision: bool = False) -> PipelineResult:
        with self.session_factory() as session:
            revision = session.get(ResumeRevision, revision_id)
            if revision is None:
                raise LookupError(f"Resume revision not found: {revision_id}")
            previous_ready = revision.status == "READY"
            candidate_id = revision.document.candidate_id
            source_path = self.blob_store.root / revision.blob.storage_path
            original_filename = revision.original_filename
            candidate_status = revision.document.candidate.status
            revision.status = "PROCESSING"
            session.commit()

        try:
            content = await asyncio.to_thread(source_path.read_bytes)
            if use_vision and self.vision_parser is not None:
                source_text = await self._vision_source_text(source_path)
                contact = extract_contact(source_text)
                parsed = await self.vision_parser.parse_resume(content, original_filename)
            else:
                extracted = await asyncio.to_thread(extract_text, source_path)
                diagnostics = {
                    "page_count": extracted.page_count, "forced_ocr": force_ocr,
                    "ai_parse_route": _ai_parse_route(self.parser),
                    "pages": [dict(
                        {key: value for key, value in asdict(page).items() if key != "text"},
                        route="ocr" if force_ocr or page.needs_ocr else "direct",
                    ) for page in extracted.pages],
                }
                self._persist_evidence(revision_id, diagnostics, extracted.text, previous_ready)
                ocr_content = content
                ocr_filename = original_filename
                if extracted.ocr_source is not None:
                    ocr_content = await asyncio.to_thread(extracted.ocr_source.read_bytes)
                    ocr_filename = extracted.ocr_source.name
                try:
                    source_text = await self._resolve_source_text(
                        ocr_content, ocr_filename, extracted, force_ocr
                    )
                finally:
                    if extracted.ocr_source is not None:
                        try:
                            extracted.ocr_source.unlink(missing_ok=True)
                        except OSError:
                            pass
                self._persist_evidence(revision_id, diagnostics, source_text, previous_ready)
                contact = extract_contact(source_text)
                parsed = await self.parser.parse_resume(source_text)
            # 模型判空不等于原文没有姓名：先用确定性规则从原文兜一层
            # （「张三先生」「Jessica Chen」这类弱信号也算）；原文确实没有线索时，
            # 只有文件名本身就是完整姓名形态（「张三.pdf」）才采用它，
            # 带岗位/年份/模板信息的文件名一律不用，最后才落中性占位。
            if not (parsed.name or "").strip():
                fallback_name = extract_name(source_text) or name_from_filename(original_filename)
                if fallback_name:
                    parsed = parsed.model_copy(update={"name": fallback_name})
            with self.session_factory() as session, session.begin():
                revision = session.get(ResumeRevision, revision_id)
                revision.review_data = parsed.model_dump(mode="json")
                overrides = dict(revision.manual_overrides or {})
                parsed = ParsedResume.model_validate({
                    **parsed.model_dump(mode="json"), **overrides,
                })
            validity = check_parsed_resume(parsed, source_text)
            if not validity.ok:
                raise PipelineFailure(validity.error_code or "E_STRUCTURED_EMPTY", validity.reason)
            candidate_id, merged_overrides = self._resolve_identity(
                revision_id, candidate_id, contact, parsed.name
            )
            if merged_overrides is not None:
                # 人工修订必须在身份识别**之后**重放：它可能来自被取代的旧版本（决策 D7：
                # 重生成解析字段、保留人工修订），而识别之前还看不到那个版本。
                overrides = merged_overrides
                parsed = ParsedResume.model_validate({
                    **parsed.model_dump(mode="json"), **overrides,
                })
            normalized = normalize_resume(parsed)
            # 初次解析产生非空画像但缺元数据时，补全 source/hash/stale；
            # 人工画像 source=manual 且 input_hash 置空，AI 画像保存输入哈希。
            if normalized.ai_profile_summary and not normalized.ai_profile_source:
                from kerui_recruit.resumes.profile import profile_input_hash
                source = "manual" if "ai_profile_summary" in overrides else "ai"
                normalized = normalized.model_copy(update={
                    "ai_profile_source": source,
                    "ai_profile_input_hash": (
                        profile_input_hash(normalized.model_dump(mode="json")) if source == "ai" else None
                    ),
                    "ai_profile_stale": False,
                })
            # Age is a derived display field rather than a ParsedResume field;
            # preserve explicit human values, including an intentional clear.
            if "age" in overrides:
                normalized = normalized.model_copy(update={"age": overrides["age"]})
            from kerui_recruit.schools.reference import SchoolReference
            from kerui_recruit.search.documents import build_candidate_document
            school_alias_groups = SchoolReference(self.session_factory).alias_groups()
            document = build_candidate_document(
                normalized.model_dump(),
                display_name=normalized.name,
                school_alias_groups=school_alias_groups,
            )
            # The runtime delegates embedding to the durable index outbox. A
            # provider outage must not discard a successfully parsed revision.
            vector = () if self.defer_indexing else tuple(
                (await self.embedding_provider.embed_documents([document["vector_text"]]))[0]
            )
            chunks = (
                SearchChunk(
                    id=new_id(),
                    candidate_id=candidate_id,
                    revision_id=revision_id,
                    content=document["keyword_text"],
                    vector=vector,
                    total_years=(
                        float(normalized.total_years) if normalized.total_years else None
                    ),
                    highest_degree=normalized.highest_degree,
                    location=normalized.location,
                    candidate_status=("AVAILABLE" if candidate_status == "PENDING_REVIEW" else candidate_status),
                    qs_rank=normalized.qs_rank,
                    school_level=normalized.school_level,
                    preferred_location=normalized.preferred_location,
                    preferred_locations=normalized.preferred_locations,
                    keyword_text=document["keyword_text"],
                    keyword_index_text=document["keyword_index_text"],
                    vector_text=document["vector_text"],
                    name_terms=tuple(document["name_terms"]),
                    school_terms=tuple(document["school_terms"]),
                    company_terms=tuple(document["company_terms"]),
                    title_terms=tuple(document["title_terms"]),
                    location_terms=tuple(document["location_terms"]),
                    skills=tuple(document["skills"]),
                    age=document["age"],
                    school_tags=tuple(document["school_tags"]),
                    direction=document.get("direction"),
                    school_region=document.get("school_region"),
                ),
            )
            self._persist_ready(
                revision_id,
                candidate_id,
                normalized,
                contact,
                source_text,
                overrides,
            )
            if self.search_index is not None:
                def update_search_projection() -> None:
                    self.search_index.delete_revision(revision_id)
                    self.search_index.upsert(list(chunks))

                await asyncio.to_thread(update_search_projection)
        except asyncio.CancelledError:
            # The worker may cancel while extraction/OCR/provider code is
            # awaiting. Compensate the already committed PROCESSING state so
            # the revision can be reviewed or explicitly reparsed.
            self._mark_failed(
                revision_id,
                "E_TASK_CANCELLED",
                "解析任务已取消，可重新发起解析",
                previous_ready,
            )
            raise
        except PipelineFailure as failure:
            self._mark_failed(revision_id, failure.code, failure.message, previous_ready)
            raise
        except ProviderError as error:
            self._mark_failed(revision_id, error.code, error.user_message, previous_ready)
            raise
        except Exception as error:  # noqa: BLE001 - record a categorized extraction failure
            self._mark_failed(
                revision_id,
                "E_EXTRACTION_FAILED",
                str(error)[:2_000] or "文档提取失败",
                previous_ready,
            )
            raise

        return PipelineResult(
            candidate_id=candidate_id,
            revision_id=revision_id,
            status="READY",
            chunks=chunks,
        )

    async def _vision_source_text(self, source_path: Path) -> str:
        """视觉解析时尽力提取文本，仅用于正文展示与联系方式识别；失败不阻断。"""
        try:
            extracted = await asyncio.to_thread(extract_text, source_path)
            return extracted.text
        except Exception:
            logger.exception("vision parse: failed to extract source text from %s", source_path)
            return ""

    async def _resolve_source_text(
        self,
        content: bytes,
        filename: str,
        extracted: ExtractedText,
        force_ocr: bool,
    ) -> str:
        """按页决定是否需要 OCR，并在页序合并后返回正文。

        普通页面保留直接提取结果，异常页面用 OCR 替换，来源信息写入日志，
        避免把水印当正文、也避免重复拼接两套正文。
        """
        pages = extracted.pages
        if not pages:
            return extracted.text

        if force_ocr:
            ocr_indexes = list(range(len(pages)))
        else:
            ocr_indexes = [page.page_index for page in pages if page.needs_ocr]

        for page in pages:
            if page.needs_ocr:
                logger.info(
                    "resume page %s needs OCR: %s (valid=%s repeated=%.0f%% dominant=%.0f%%)",
                    page.page_index + 1,
                    page.reason,
                    page.valid_char_count,
                    page.repeated_ratio * 100,
                    page.dominant_ratio * 100,
                )

        if not ocr_indexes:
            return extracted.text

        if self.ocr_provider is None:
            raise PipelineFailure(
                "E_OCR_REQUIRED",
                "该文件需要 OCR 识别，但尚未配置 OCR 服务，无法继续解析",
            )

        ocr_texts = await self._ocr_pages(content, filename, ocr_indexes)
        if len(ocr_texts) != len(ocr_indexes):
            raise PipelineFailure("E_OCR_INCOMPLETE", "OCR 返回页数不完整，请人工复核")
        ocr_map: dict[int, str] = {}
        for index, text in zip(ocr_indexes, ocr_texts, strict=True):
            self._validate_ocr_page(index, text)
            ocr_map[index] = text

        merged: list[str] = []
        for page in pages:
            text = ocr_map.get(page.page_index, page.text)
            if text.strip():
                merged.append(text)
        return "\n".join(merged)

    async def _ocr_pages(
        self,
        content: bytes,
        filename: str,
        page_indexes: list[int],
    ) -> list[str]:
        if hasattr(self.ocr_provider, "extract_pages"):
            return await self.ocr_provider.extract_pages(content, filename, page_indexes)
        # Legacy providers must receive the requested page only: reusing the
        # whole document for each page duplicates and misorders resume evidence.
        results = []
        with pymupdf.open(stream=content, filetype="pdf") as document:
            for index in page_indexes:
                with pymupdf.open() as single:
                    single.insert_pdf(document, from_page=index, to_page=index)
                    page_content = single.tobytes()
                results.append(await self.ocr_provider.extract(page_content, filename))
        return results

    @staticmethod
    def _validate_ocr_page(page_index: int, text: str) -> None:
        if not text or not text.strip():
            raise PipelineFailure("E_OCR_EMPTY", f"第 {page_index + 1} 页 OCR 未识别到文字")
        quality = analyze_text(text)
        if quality.valid_char_count == 0 or quality.dominant_ratio >= DOMINANT_FRAGMENT_RATIO:
            raise PipelineFailure(
                "E_OCR_LOW_QUALITY",
                f"第 {page_index + 1} 页 OCR 结果没有有效正文或仍以重复水印为主，请人工复核",
            )

    def _resolve_identity(
        self,
        revision_id: str,
        current_candidate_id: str,
        contact,
        name: str | None,
    ) -> tuple[str, dict | None]:
        """解析后按联系方式指纹识别已有候选人，命中则把新版本改挂到已有候选。

        返回 ``(候选人 id, 该版本最终生效的人工修订)``。第二个元素为 ``None`` 表示
        「无需变更」，调用方必须保留原样——**不能**把它当成空 dict，否则会把本版本
        自己的人工覆盖清空，导致 ``_persist_ready`` 的冲突校验误报。

        人工修订来自被取代的旧版本（决策 D7：重生成解析字段、保留人工修订），调用方
        需要用它在解析结果上重放，否则「重生成一切信息」会把人工校正一并丢掉。

        身份识别为尽力而为：任何异常都不阻断简历解析，退回原候选人。

        旧版本的物理清理由:meth:`_persist_ready` 在**同一事务**内完成，避免出现
        「旧版本已删、新版本仍不可用」的空洞状态。
        """
        try:
            with self.session_factory() as session:
                revision = session.get(ResumeRevision, revision_id)
                if revision is None:
                    return current_candidate_id, None

                resolution = resolve_identity(
                    session,
                    phone=contact.phone,
                    email=contact.email,
                    name=name,
                )
                if resolution.action != "MATCHED" or resolution.candidate_id is None:
                    return current_candidate_id, None
                target_id = resolution.candidate_id

                # 保留人工修订：迁移到新版本；本版本自己的覆盖（同一版本重新解析时产生）
                # 更新，因此优先级更高。
                inherited = inherit_manual_overrides(
                    session, candidate_id=target_id, keep_revision_id=revision_id
                )
                own = dict(revision.manual_overrides or {})
                merged = {**inherited, **own}
                if merged != own:
                    revision.manual_overrides = merged

                if target_id == current_candidate_id:
                    session.commit()
                    return target_id, merged

                target_document = session.scalar(
                    select(ResumeDocument)
                    .where(ResumeDocument.candidate_id == target_id)
                    .order_by(ResumeDocument.created_at)
                    .limit(1)
                )
                if target_document is None:
                    target_document = ResumeDocument(candidate_id=target_id)
                    session.add(target_document)
                    session.flush()

                old_document_id = revision.document_id
                revision.document_id = target_document.id
                session.flush()

                remaining = session.scalar(
                    select(ResumeRevision.id)
                    .where(ResumeRevision.document_id == old_document_id)
                    .limit(1)
                )
                if remaining is None:
                    old_document = session.get(ResumeDocument, old_document_id)
                    if old_document is not None:
                        session.delete(old_document)

                # 占位候选人是本次导入临时创建的：身份已并入目标候选人，直接物理删除
                # （决策 D8：不用软删除），免得库里留下永远无人使用的空候选人。
                duplicate = session.get(Candidate, current_candidate_id)
                if duplicate is not None:
                    session.delete(duplicate)

                for claim in session.scalars(
                    select(ResumeImportClaim).where(
                        ResumeImportClaim.revision_id == revision_id
                    )
                ).all():
                    claim.candidate_id = target_id

                session.commit()
                return target_id, merged
        except Exception:
            logger.exception(
                "identity resolution failed; keeping candidate %s",
                current_candidate_id,
            )
            return current_candidate_id, None

    def _persist_ready(
        self,
        revision_id: str,
        candidate_id: str,
        normalized: NormalizedResume,
        contact,
        source_text: str,
        expected_overrides: dict,
    ) -> None:
        with self.session_factory() as session, session.begin():
            revision = session.get(ResumeRevision, revision_id)
            if revision is None:
                raise LookupError(f"Resume revision not found: {revision_id}")
            candidate = session.get(Candidate, candidate_id)
            if candidate is None:
                raise LookupError(f"Candidate not found: {candidate_id}")
            if (revision.manual_overrides or {}) != expected_overrides:
                raise PipelineFailure(
                    "E_REPARSE_CONFLICT", "解析期间有人工修改，已保留人工修订，请重新解析", retryable=True,
                )
            if revision.is_current:
                candidate.display_name = normalized.name or UNKNOWN_NAME
                candidate.total_years = normalized.total_years
                candidate.highest_degree = normalized.highest_degree
                if candidate.status == "PENDING_REVIEW":
                    candidate.status = "AVAILABLE"
            if revision.is_current and self.encryption_service is not None and (contact.email or contact.phone):
                existing = session.scalar(
                    select(CandidateContact).where(
                        CandidateContact.candidate_id == candidate_id
                    )
                )
                if existing is None:
                    existing = CandidateContact(candidate=candidate)
                    session.add(existing)
                manual_fields = existing.manual_fields or []
                if "email" not in manual_fields and existing.email_confidence != 1.0:
                    existing.email_encrypted = (
                        self.encryption_service.encrypt(contact.email)
                        if contact.email else None
                    )
                    existing.email_confidence = 0.9 if contact.email else None
                    existing.email_fingerprint = normalize_email(contact.email)
                if "phone" not in manual_fields and existing.phone_confidence != 1.0:
                    existing.phone_encrypted = (
                        self.encryption_service.encrypt(contact.phone)
                        if contact.phone else None
                    )
                    existing.phone_confidence = 0.9 if contact.phone else None
                    existing.phone_fingerprint = normalize_phone(contact.phone)
            revision.raw_text = source_text
            parsed_data = normalized.model_dump(mode="json")
            revision.parsed_data = parsed_data
            revision.parse_version = "resume-schema-v1"
            revision.status = "READY"
            revision.error_code = None
            revision.error_message = None
            revision.display_name = build_display_name(
                normalized, Path(revision.original_filename).suffix.lower()
            )
            if revision.is_current:
                from kerui_recruit.search.sync import enqueue_sync
                enqueue_sync(session, "candidate", candidate.id)
                # 只保留当前简历（决策 D8）：被取代的旧版本在这里物理删除。与本事务里
                # 「新版本置为 READY、候选人列改写」一起提交，避免中途失败留下
                # 「旧版本已删、新版本仍不可用」的空洞状态。
                # 索引无需单独处理：上面的 enqueue_sync 会整体重建该候选人，而
                # replace_candidate 会先删光其全部 chunk 再按当前版本写入。
                purge_superseded_revisions(
                    session,
                    candidate_id=candidate.id,
                    keep_revision_id=revision.id,
                    blob_store=self.blob_store,
                    task_repository=self.task_repository,
                )

    def _persist_evidence(self, revision_id: str, diagnostics: dict, text: str, previous_ready: bool) -> None:
        with self.session_factory() as session, session.begin():
            revision = session.get(ResumeRevision, revision_id)
            revision.extraction_diagnostics = {**diagnostics, "source_text": text}
            if not previous_ready:
                revision.raw_text = text

    def _mark_failed(
        self,
        revision_id: str,
        code: str,
        message: str,
        previous_ready: bool,
    ) -> None:
        with self.session_factory() as session, session.begin():
            revision = session.get(ResumeRevision, revision_id)
            if revision is None:
                return
            if previous_ready:
                # 重新解析失败：保留上一次有效画像与索引，仅由任务记录本次失败。
                revision.status = "READY"
                revision.error_code = None
                revision.error_message = None
            else:
                revision.status = "FAILED"
                revision.error_code = code
                revision.error_message = message[:2_000]
            revision.extraction_diagnostics = {
                **(revision.extraction_diagnostics or {}),
                "last_error": {"code": code, "message": message[:2_000]},
            }
