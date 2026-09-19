from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeRevision, TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.resumes.ingest import (
    IngestResume,
    ResumeIngestService,
    UnsupportedResumeType,
)
from kerui_recruit.storage.blobs import BlobStore


def make_service(tmp_path: Path) -> tuple[ResumeIngestService, Session]:
    engine = create_engine_for(tmp_path / "db" / "recruit.sqlite3")
    migrate(engine)
    session = Session(engine)
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    return ResumeIngestService(session, store), session


def test_ingest_identical_content_is_already_imported(tmp_path: Path) -> None:
    """相同文件（即使文件名不同）只导入一次，不重复创建版本和任务。"""
    service, session = make_service(tmp_path)
    with session:
        first = service.ingest(
            IngestResume(filename="张三.pdf", content=b"same", display_name="张三")
        )
        second = service.ingest(
            IngestResume(
                filename="张三-更新.pdf",
                content=b"same",
                candidate_id=first.candidate_id,
            )
        )

        assert first.action == "CREATED"
        assert second.action == "ALREADY_IMPORTED"
        assert second.candidate_id == first.candidate_id
        assert second.revision_id == first.revision_id
        assert session.scalar(select(func.count()).select_from(Blob)) == 1
        assert session.get(Blob, first.blob_id).reference_count == 1
        assert session.scalar(select(func.count()).select_from(ResumeRevision)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 1


def test_ingest_creates_candidate_and_pending_parse_task(tmp_path: Path) -> None:
    """An accepted original must always have a recoverable fact row and task."""
    service, session = make_service(tmp_path)
    with session:
        result = service.ingest(
            IngestResume(filename="李四.docx", content=b"docx", display_name="李四")
        )

        candidate = session.get(Candidate, result.candidate_id)
        task = session.get(TaskRecord, result.task_id)
        assert candidate is not None and candidate.display_name == "李四"
        assert task is not None and task.status == "PENDING"
        assert task.payload == {"revision_id": result.revision_id, "passive_match": True}


def test_unsupported_file_type_is_rejected_before_writing(tmp_path: Path) -> None:
    """Unsupported executable content must never enter the original store."""
    service, session = make_service(tmp_path)
    with session, pytest.raises(UnsupportedResumeType) as error:
        service.ingest(IngestResume(filename="payload.exe", content=b"bad"))

    assert error.value.code == "E_FILE_TYPE_UNSUPPORTED"
    assert list((tmp_path / "blobs").rglob("*.*")) == []


def test_ingest_routes_to_configured_queue(tmp_path: Path) -> None:
    """Interactive uploads must land in the fast queue, not the batch queue."""
    service, session = make_service(tmp_path)
    with session:
        result = service.ingest(
            IngestResume(filename="张三.pdf", content=b"pdf", queue_name="interactive")
        )
        task = session.get(TaskRecord, result.task_id)

        assert task is not None
        assert task.queue_name == "interactive"
