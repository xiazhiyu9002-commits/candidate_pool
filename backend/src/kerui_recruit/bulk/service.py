"""批量操作：复用单条业务服务，逐项执行并返回逐项结果。

设计边界：
- 只传稳定实体 ID（candidate_id / jd_id / case_id / revision_id），不信任前端当前滚动行；
- 单个项目失败不影响整批，返回逐项 ``ok`` / ``error``；
- 删除永久生效，调用方必须在 UI 层明确确认数量与影响范围。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import (
    Candidate,
    CandidateJobCase,
    Jd,
    ResumeDocument,
    ResumeRevision,
)
from kerui_recruit.jd.deletion import JdDeletionService
from kerui_recruit.resumes.deletion import CandidateDeletionService
from kerui_recruit.tasks.repository import TaskSpec, reparse_idempotency_key

# 单次批量上限，避免一次请求删除/处理过多实体造成长时间阻塞或误操作。
BULK_LIMIT = 500


@dataclass(frozen=True, slots=True)
class BulkItemResult:
    entity_id: str
    ok: bool
    error: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BulkResult:
    results: list[BulkItemResult]

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return len(self.results) - self.succeeded


def _safe_name(filename: str | None, fallback: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename or fallback).stem or "file").strip(" .")
    return stem[:80] or "file"


def bulk_delete_candidates(
    services,
    candidate_ids: list[str],
) -> BulkResult:
    deletion = CandidateDeletionService(
        services.session_factory,
        services.search_service.index,
        blob_store=services.blob_store,
        task_repository=services.task_repository,
    )
    results: list[BulkItemResult] = []
    for candidate_id in dict.fromkeys(candidate_ids):
        try:
            deleted = deletion.delete(candidate_id)
            results.append(BulkItemResult(candidate_id, ok=deleted, error=None if deleted else "候选人不存在"))
        except Exception as exc:  # noqa: BLE001 - per-item failure must not abort batch
            results.append(BulkItemResult(candidate_id, ok=False, error=type(exc).__name__))
    return BulkResult(results=results)


def bulk_delete_jds(services, jd_ids: list[str]) -> BulkResult:
    jd_index = services.index_sync_service.jd_index if services.index_sync_service else None
    deletion = JdDeletionService(services.session_factory, jd_index=jd_index)
    results: list[BulkItemResult] = []
    for jd_id in dict.fromkeys(jd_ids):
        try:
            # 删除前统计关联流程数量：这些流程**会被保留**（岗位信息转为快照），
            # 供 UI 明确告知影响范围。
            with services.session_factory() as session:
                case_count = _count_cases(session, jd_id)
            deleted = deletion.delete(jd_id)
            results.append(BulkItemResult(
                jd_id, ok=deleted, error=None if deleted else "岗位不存在",
                extra={"retained_cases": case_count} if deleted else {},
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(BulkItemResult(jd_id, ok=False, error=type(exc).__name__))
    return BulkResult(results=results)


def _count_cases(session: Session, jd_id: str) -> int:
    from sqlalchemy import func
    return int(session.scalar(
        select(func.count()).select_from(CandidateJobCase).where(
            CandidateJobCase.jd_id == jd_id, CandidateJobCase.deleted_at.is_(None)
        )
    ) or 0)


def bulk_delete_cases(services, case_ids: list[str]) -> BulkResult:
    results: list[BulkItemResult] = []
    for case_id in dict.fromkeys(case_ids):
        try:
            services.case_service.delete(case_id)
            results.append(BulkItemResult(case_id, ok=True))
        except LookupError:
            results.append(BulkItemResult(case_id, ok=False, error="流程不存在"))
        except Exception as exc:  # noqa: BLE001
            results.append(BulkItemResult(case_id, ok=False, error=type(exc).__name__))
    return BulkResult(results=results)


def _bulk_enqueue_reparse(
    services,
    revision_ids: list[str],
    *,
    force_ocr: bool,
    use_vision: bool,
) -> BulkResult:
    """对选中版本逐条入队重新解析；单条失败不影响整批。

    幂等键复用单条 reparse 的规则：进行中的同名任务保持幂等，
    已是终态的同名任务换新键，保证「再点一次」真的会重跑。
    """
    results: list[BulkItemResult] = []
    mode = "vision" if use_vision else "ocr"
    for revision_id in dict.fromkeys(revision_ids):
        try:
            with services.session_factory() as session:
                revision = session.get(ResumeRevision, revision_id)
                if revision is None:
                    results.append(BulkItemResult(revision_id, ok=False, error="简历版本不存在"))
                    continue
                key = reparse_idempotency_key(session, revision_id, mode)
            task_id = services.task_repository.enqueue(
                TaskSpec(
                    task_type="PARSE_RESUME",
                    queue_name="batch",
                    priority=5,
                    payload={
                        "revision_id": revision_id,
                        "force_ocr": force_ocr,
                        "use_vision": use_vision,
                    },
                    idempotency_key=key,
                )
            )
            results.append(BulkItemResult(revision_id, ok=True, extra={"task_id": task_id}))
        except Exception as exc:  # noqa: BLE001
            results.append(BulkItemResult(revision_id, ok=False, error=type(exc).__name__))
    return BulkResult(results=results)


def bulk_reparse(services, revision_ids: list[str]) -> BulkResult:
    """普通重新解析（force_ocr=false、use_vision=true）。

    适用于「解析结果不理想」的常规重试：只有异常页走 OCR，正常页仍走文本/视觉解析。
    """
    return _bulk_enqueue_reparse(services, revision_ids, force_ocr=False, use_vision=True)


def bulk_force_ocr(services, revision_ids: list[str]) -> BulkResult:
    """强制 OCR 重新解析（force_ocr=true、use_vision=false）。

    扫描件/文本层乱码简历专用：整份走 OCR，指定识别文本。
    """
    return _bulk_enqueue_reparse(services, revision_ids, force_ocr=True, use_vision=False)


def _candidate_latest_blob(session: Session, candidate_id: str):
    """返回候选人当前 READY 修订的原始文件名与存储路径（无则 None）。"""
    revision = session.scalar(
        select(ResumeRevision)
        .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
        .where(
            ResumeDocument.candidate_id == candidate_id,
            ResumeRevision.is_current.is_(True),
            ResumeRevision.status == "READY",
        )
        .order_by(ResumeRevision.created_at.desc(), ResumeRevision.id.desc())
        .limit(1)
    )
    if revision is None or revision.blob is None:
        return None
    return revision


def bulk_download_candidates(services, candidate_ids: list[str]) -> tuple[bytes, BulkResult]:
    """把选中候选人的最新有效原件打包为 ZIP，文件名冲突追加匿名序号，缺文件列入结果。"""
    buffer = io.BytesIO()
    results: list[BulkItemResult] = []
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for candidate_id in dict.fromkeys(candidate_ids):
            try:
                with services.session_factory() as session:
                    revision = _candidate_latest_blob(session, candidate_id)
                    if revision is None:
                        results.append(BulkItemResult(candidate_id, ok=False, error="无有效原件"))
                        continue
                    source = services.blob_store.root / revision.blob.storage_path
                    filename = revision.original_filename or f"{candidate_id}.dat"
                if not source.exists():
                    results.append(BulkItemResult(candidate_id, ok=False, error="原始文件缺失"))
                    continue
                suffix = Path(filename).suffix or ".dat"
                stem = _safe_name(filename, candidate_id)
                used_names[stem] = used_names.get(stem, 0) + 1
                seq = used_names[stem]
                arcname = f"{stem}_{seq:03d}{suffix}" if seq > 1 else f"{stem}{suffix}"
                archive.write(source, arcname=arcname)
                results.append(BulkItemResult(candidate_id, ok=True, extra={"arcname": arcname}))
            except Exception as exc:  # noqa: BLE001
                results.append(BulkItemResult(candidate_id, ok=False, error=type(exc).__name__))
    buffer.seek(0)
    return buffer.getvalue(), BulkResult(results=results)
