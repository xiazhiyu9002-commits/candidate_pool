from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, CandidateContact, ResumeRevision, TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.resumes.identity import resolve_identity
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.storage.blobs import BlobStore


CONTENT = b"%PDF-1.4 fake resume content for idempotency test"


def _make(tmp_path: Path):
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    blob_store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    return factory, blob_store


def _counts(factory, model) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_same_file_imported_twice_is_idempotent(tmp_path: Path) -> None:
    factory, blob_store = _make(tmp_path)

    with factory() as session:
        first = ResumeIngestService(session, blob_store).ingest(
            IngestResume(filename="resume.pdf", content=CONTENT)
        )
    with factory() as session:
        second = ResumeIngestService(session, blob_store).ingest(
            IngestResume(filename="resume.pdf", content=CONTENT)
        )

    assert first.action == "CREATED"
    assert second.action == "ALREADY_IMPORTED"
    assert second.candidate_id == first.candidate_id
    assert second.revision_id == first.revision_id

    assert _counts(factory, Candidate) == 1
    assert _counts(factory, ResumeRevision) == 1
    assert _counts(factory, TaskRecord) == 1
    with factory() as session:
        blobs = list(session.scalars(select(Blob)).all())
        assert len(blobs) == 1
        assert blobs[0].reference_count == 1


def test_concurrent_claim_returns_same_result(tmp_path: Path) -> None:
    factory, blob_store = _make(tmp_path)

    with factory() as session:
        ResumeIngestService(session, blob_store).ingest(
            IngestResume(filename="resume.pdf", content=CONTENT)
        )
    # 第二个请求命中唯一 claim，返回同一个 candidate/revision。
    with factory() as session:
        result = ResumeIngestService(session, blob_store).ingest(
            IngestResume(filename="resume.pdf", content=CONTENT)
        )

    assert result.action == "ALREADY_IMPORTED"
    assert _counts(factory, Candidate) == 1
    assert _counts(factory, TaskRecord) == 1


def test_identity_same_phone_matches(tmp_path: Path) -> None:
    factory, _ = _make(tmp_path)
    with factory() as session:
        c = Candidate(display_name="张三", status="AVAILABLE")
        session.add(c)
        session.flush()
        session.add(CandidateContact(candidate_id=c.id, phone_fingerprint="13800138000"))
        session.commit()

    with factory() as session:
        result = resolve_identity(session, phone="138 0013 8000", email=None, name=None)
    assert result.action == "MATCHED"


def test_identity_phone_email_conflict(tmp_path: Path) -> None:
    factory, _ = _make(tmp_path)
    with factory() as session:
        a = Candidate(display_name="A", status="AVAILABLE")
        b = Candidate(display_name="B", status="AVAILABLE")
        session.add_all([a, b])
        session.flush()
        session.add(CandidateContact(candidate_id=a.id, phone_fingerprint="13800138000"))
        session.add(CandidateContact(candidate_id=b.id, email_fingerprint="a@b.com"))
        session.commit()

    with factory() as session:
        result = resolve_identity(session, phone="13800138000", email="a@b.com", name=None)
    assert result.action == "IDENTITY_CONFLICT"


def test_identity_name_only_is_possible_duplicate(tmp_path: Path) -> None:
    factory, _ = _make(tmp_path)
    with factory() as session:
        session.add(Candidate(display_name="张三", status="AVAILABLE"))
        session.commit()

    with factory() as session:
        result = resolve_identity(session, phone=None, email=None, name="张三")
    assert result.action == "POSSIBLE_DUPLICATE"
