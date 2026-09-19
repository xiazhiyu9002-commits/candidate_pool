from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import SCHEMA_VERSION, migrate
from kerui_recruit.db.models import (
    Blob,
    Candidate,
    CandidateContact,
    ResumeDocument,
    ResumeRevision,
    SchemaVersion,
    TaskRecord,
)
from kerui_recruit.db.session import (
    UnsupportedSQLiteVersion,
    assert_supported_sqlite_version,
    create_engine_for,
)


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_engine_enables_foreign_keys_wal_and_full_synchronous(tmp_path: Path) -> None:
    """Dropping a SQLite guard could allow corrupt or orphaned local data."""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5_000


def test_unsafe_sqlite_wal_version_is_rejected() -> None:
    """A vulnerable embedded SQLite build must not reach production startup."""
    with pytest.raises(UnsupportedSQLiteVersion) as error:
        assert_supported_sqlite_version((3, 34, 9))

    assert error.value.code == "E_SQLITE_VERSION_UNSAFE"
    assert_supported_sqlite_version((3, 35, 0))


def test_candidate_document_revision_and_blob_relationships(tmp_path: Path) -> None:
    """Losing an ownership link would make a resume version unrecoverable."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        blob = Blob(
            content_sha256="a" * 64,
            suffix=".pdf",
            size_bytes=128,
            storage_path="aa/aa/" + "a" * 64 + ".pdf",
        )
        candidate = Candidate(display_name="张三", total_years=Decimal("5.0"))
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(
            document=document,
            blob=blob,
            content_sha256=blob.content_sha256,
            original_filename="张三简历.pdf",
            status="PENDING",
        )
        session.add(candidate)
        session.commit()

        loaded = session.scalars(select(ResumeRevision)).one()
        assert loaded.document.candidate.display_name == "张三"
        assert loaded.blob.content_sha256 == "a" * 64
        assert loaded.document.candidate.deleted_at is None
        assert loaded.document.candidate.total_years == Decimal("5.0")


def test_task_defaults_are_durable_and_queryable(tmp_path: Path) -> None:
    """A task without a persisted initial state cannot recover after a crash."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        task = TaskRecord(
            task_type="PARSE_RESUME",
            queue_name="batch",
            priority=10,
            payload={"revision_id": "revision-1"},
            idempotency_key="parse:revision-1:v1",
        )
        session.add(task)
        session.commit()

        loaded = session.get(TaskRecord, task.id)
        assert loaded is not None
        assert loaded.status == "PENDING"
        assert loaded.progress == 0
        assert loaded.attempts == 0


def test_candidate_contact_is_one_to_one_and_encrypted_fields_are_nullable(
    tmp_path: Path,
) -> None:
    """Each candidate has at most one contact record with encrypted fields."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        candidate = Candidate(display_name="张三")
        contact = CandidateContact(
            candidate=candidate,
            email_encrypted="cipher-email",
            phone_encrypted="cipher-phone",
            email_confidence=0.9,
            phone_confidence=0.8,
        )
        session.add(candidate)
        session.commit()

        loaded = session.scalars(select(CandidateContact)).one()
        assert loaded.candidate.display_name == "张三"
        assert loaded.email_encrypted == "cipher-email"
        assert loaded.phone_encrypted == "cipher-phone"
        assert loaded.email_confidence == pytest.approx(0.9)
        assert loaded.phone_confidence == pytest.approx(0.8)
        assert loaded.candidate_id == candidate.id


def test_migrate_records_schema_version(tmp_path: Path) -> None:
    """The database must carry a schema version for future migrations."""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)

    with Session(engine) as session:
        version = session.scalar(select(SchemaVersion))
        assert version is not None
        assert version.version == SCHEMA_VERSION


def test_migrate_rejects_newer_schema_version(tmp_path: Path) -> None:
    """A database created by a newer app must not be opened by an older one."""
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    with Session(engine) as session, session.begin():
        session.add(SchemaVersion(version=99))

    with pytest.raises(RuntimeError, match="高于当前支持"):
        migrate(engine)
