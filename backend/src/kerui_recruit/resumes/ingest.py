from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kerui_recruit.db.models import (
    Blob,
    Candidate,
    ResumeDocument,
    ResumeImportClaim,
    ResumeRevision,
    TaskRecord,
)
from kerui_recruit.resumes.extract import UNKNOWN_NAME
from kerui_recruit.storage.blobs import BlobStore


class UnsupportedResumeType(ValueError):
    code = "E_FILE_TYPE_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class IngestResume:
    filename: str
    content: bytes
    display_name: str | None = None
    candidate_id: str | None = None
    queue_name: str = "batch"
    passive_match: bool = True


@dataclass(frozen=True, slots=True)
class IngestResult:
    action: str  # CREATED / UPDATED / ALREADY_IMPORTED / DUPLICATE_CONFLICT
    candidate_id: str | None
    document_id: str | None
    revision_id: str | None
    blob_id: str | None
    task_id: str | None
    message: str = ""
    conflict_candidate_ids: list[str] = field(default_factory=list)
    created_task: bool = False


class ResumeIngestService:
    allowed_suffixes = frozenset({".pdf", ".doc", ".docx"})

    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self.session = session
        self.blob_store = blob_store

    def ingest(self, command: IngestResume) -> IngestResult:
        suffix = Path(command.filename).suffix.lower()
        if suffix not in self.allowed_suffixes:
            raise UnsupportedResumeType(f"Unsupported resume type: {suffix or 'none'}")
        sha256 = hashlib.sha256(command.content).hexdigest()

        claim = self.session.scalar(
            select(ResumeImportClaim).where(ResumeImportClaim.content_sha256 == sha256)
        )
        if claim is not None and claim.status == "SUCCESS":
            return IngestResult(
                action="ALREADY_IMPORTED",
                candidate_id=claim.candidate_id,
                revision_id=claim.revision_id,
                document_id=None,
                blob_id=None,
                task_id=None,
                message="该文件已经导入过",
            )

        existing = self._existing_by_sha(sha256)
        if existing is not None:
            return existing

        # 新文件：写 blob + 建立候选/文档/版本/任务，并用唯一 claim 作为并发门闩。
        stored = self.blob_store.put(BytesIO(command.content), suffix)
        try:
            blob = self.session.scalar(select(Blob).where(Blob.content_sha256 == stored.sha256))
            if blob is None:
                blob = Blob(
                    content_sha256=stored.sha256,
                    suffix=stored.suffix,
                    size_bytes=stored.size_bytes,
                    storage_path=str(stored.path.relative_to(self.blob_store.root)),
                )
                self.session.add(blob)
                self.session.flush()
            else:
                blob.reference_count += 1

            candidate = self._resolve_candidate(command)
            document = self._resolve_document(candidate)
            self.session.execute(
                update(ResumeRevision)
                .where(ResumeRevision.document_id == document.id)
                .values(is_current=False)
            )
            revision = ResumeRevision(
                document=document,
                blob=blob,
                content_sha256=stored.sha256,
                original_filename=command.filename,
                status="PENDING",
                is_current=True,
            )
            self.session.add(revision)
            self.session.flush()
            task = TaskRecord(
                task_type="PARSE_RESUME",
                queue_name=command.queue_name,
                priority=10,
                payload={"revision_id": revision.id, "passive_match": command.passive_match},
                idempotency_key=f"PARSE_RESUME:{revision.id}:v1",
            )
            self.session.add(task)
            self.session.add(
                ResumeImportClaim(
                    content_sha256=stored.sha256,
                    candidate_id=candidate.id,
                    revision_id=revision.id,
                    status="SUCCESS",
                )
            )
            self.session.commit()
            return IngestResult(
                action="CREATED",
                candidate_id=candidate.id,
                document_id=document.id,
                revision_id=revision.id,
                blob_id=blob.id,
                task_id=task.id,
                message="已创建候选人并进入解析队列",
                created_task=True,
            )
        except IntegrityError:
            # 并发下另一个请求已抢先声明该哈希：读取胜者的结果。
            self.session.rollback()
            winner = self.session.scalar(
                select(ResumeImportClaim).where(ResumeImportClaim.content_sha256 == sha256)
            )
            if winner is not None and winner.status == "SUCCESS":
                return IngestResult(
                    action="ALREADY_IMPORTED",
                    candidate_id=winner.candidate_id,
                    revision_id=winner.revision_id,
                    document_id=None,
                    blob_id=None,
                    task_id=None,
                    message="该文件已经导入过",
                )
            raise
        except Exception:
            self.session.rollback()
            if stored.created:
                stored.path.unlink(missing_ok=True)
            raise

    def _existing_by_sha(self, sha256: str) -> IngestResult | None:
        revisions = list(
            self.session.scalars(
                select(ResumeRevision).where(ResumeRevision.content_sha256 == sha256)
            ).all()
        )
        if not revisions:
            return None
        candidate_ids = list(dict.fromkeys(r.document.candidate_id for r in revisions))
        if len(candidate_ids) > 1:
            return IngestResult(
                action="DUPLICATE_CONFLICT",
                candidate_id=None,
                document_id=None,
                revision_id=None,
                blob_id=None,
                task_id=None,
                message="相同文件已关联到多个候选人，请人工处理",
                conflict_candidate_ids=candidate_ids,
            )
        current = next((r for r in revisions if r.is_current), None)
        if current is not None:
            return IngestResult(
                action="ALREADY_IMPORTED",
                candidate_id=candidate_ids[0],
                revision_id=current.id,
                document_id=None,
                blob_id=None,
                task_id=None,
                message="该文件已经是当前版本",
            )
        return IngestResult(
            action="ALREADY_IMPORTED",
            candidate_id=candidate_ids[0],
            revision_id=revisions[0].id,
            document_id=None,
            blob_id=None,
            task_id=None,
            message="该文件已存在于版本历史中",
        )

    def _resolve_candidate(self, command: IngestResume) -> Candidate:
        if command.candidate_id:
            candidate = self.session.get(Candidate, command.candidate_id)
            if candidate is None:
                raise LookupError(f"Candidate not found: {command.candidate_id}")
            return candidate
        # 解析前先给一个中性占位：文件名不是姓名，解析完成后会被真实姓名覆盖。
        display_name = command.display_name or UNKNOWN_NAME
        candidate = Candidate(display_name=display_name)
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def _resolve_document(self, candidate: Candidate) -> ResumeDocument:
        document = self.session.scalar(
            select(ResumeDocument)
            .where(ResumeDocument.candidate_id == candidate.id)
            .order_by(ResumeDocument.created_at)
            .limit(1)
        )
        if document is not None:
            return document
        document = ResumeDocument(candidate=candidate)
        self.session.add(document)
        self.session.flush()
        return document
