from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.migration.service import MigrationError, MigrationService


@pytest.fixture
def current_root(tmp_path: Path) -> Path:
    root = tmp_path / "current"
    (root / "db").mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    (root / "search").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    return root


def _seed_source(root: Path) -> sessionmaker[Session]:
    engine = create_engine_for(root / "db" / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(Candidate(display_name="张三"))
        session.commit()
    engine.dispose()
    (root / "blobs" / "a.bin").write_bytes(b"hello")
    (root / "search" / "index.bin").write_bytes(b"world")
    (root / "config" / "settings.json").write_text("{}")
    return factory


def _snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def test_migrate_to_copies_and_verifies(
    tmp_path: Path, current_root: Path
) -> None:
    factory = _seed_source(current_root)

    service = MigrationService(
        session_factory=factory,
        current_root=current_root,
    )
    target = tmp_path / "target"

    report = service.migrate_to(str(target))

    assert report.ok is True
    assert report.candidate_count == 1
    assert report.files_copied == report.files_verified
    assert report.files_copied > 0
    assert (target / "blobs" / "a.bin").read_bytes() == b"hello"
    assert not (target / "search" / "index.bin").exists()
    assert (target / ".search-rebuild-required").exists()
    assert (target / "config" / "settings.json").read_text() == "{}"
    # staging 目录在成功后已提升为 target，不应残留
    assert list(tmp_path.glob(".target.staging-*")) == []


def test_migrate_to_rejects_same_path(
    tmp_path: Path, current_root: Path
) -> None:
    factory = _seed_source(current_root)
    before = _snapshot(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(current_root))

    assert error.value.code == "E_MIGRATION_SAME_PATH"
    assert _snapshot(current_root) == before


def test_migrate_to_rejects_target_inside_source(
    tmp_path: Path, current_root: Path
) -> None:
    factory = _seed_source(current_root)
    before = _snapshot(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)
    target = current_root / "sub"

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(target))

    assert error.value.code == "E_MIGRATION_TARGET_INSIDE_SOURCE"
    assert _snapshot(current_root) == before


def test_migrate_to_rejects_source_inside_target(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    current_root = parent / "current"
    (current_root / "db").mkdir(parents=True)
    (current_root / "blobs").mkdir(parents=True)
    (current_root / "search").mkdir(parents=True)
    (current_root / "config").mkdir(parents=True)
    factory = _seed_source(current_root)
    before = _snapshot(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(parent))

    assert error.value.code == "E_MIGRATION_SOURCE_INSIDE_TARGET"
    assert _snapshot(current_root) == before


def test_migrate_to_rejects_non_empty_target(
    tmp_path: Path, current_root: Path
) -> None:
    factory = _seed_source(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)
    target = tmp_path / "target"
    (target / "db").mkdir(parents=True)
    (target / "db" / "existing.sqlite3").write_bytes(b"existing-data")
    before = _snapshot(target)

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(target))

    assert error.value.code == "E_MIGRATION_TARGET_NOT_EMPTY"
    assert error.value.status_code == 409
    assert _snapshot(target) == before


def test_migrate_to_copy_failure_leaves_source_and_target_unchanged(
    tmp_path: Path, current_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _seed_source(current_root)
    before = _snapshot(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)
    target = tmp_path / "target"

    def _boom(self: MigrationService, source_root: Path, staging: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(MigrationService, "_copy_essential_dirs", _boom)

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(target))

    assert error.value.code == "E_MIGRATION_FAILED"
    assert not target.exists()
    assert _snapshot(current_root) == before
    assert list(tmp_path.glob(".target.staging-*")) == []


def test_migrate_to_verify_failure_leaves_source_and_target_unchanged(
    tmp_path: Path, current_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _seed_source(current_root)
    before = _snapshot(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)
    target = tmp_path / "target"

    def _copy_then_corrupt(
        self: MigrationService, source_root: Path, staging: Path
    ) -> None:
        for name in ("db", "search", "blobs", "config"):
            source = source_root / name
            if source.exists():
                shutil.copytree(source, staging / name)
        (staging / "blobs" / "a.bin").write_bytes(b"CORRUPTED")

    monkeypatch.setattr(MigrationService, "_copy_essential_dirs", _copy_then_corrupt)

    with pytest.raises(MigrationError) as error:
        service.migrate_to(str(target))

    assert error.value.code == "E_MIGRATION_VERIFY_FAILED"
    assert not target.exists()
    assert _snapshot(current_root) == before
    assert list(tmp_path.glob(".target.staging-*")) == []


def test_migration_uses_frozen_database_while_live_source_changes(tmp_path, current_root, monkeypatch):
    import sqlite3
    from contextlib import closing
    factory = _seed_source(current_root)
    service = MigrationService(session_factory=factory, current_root=current_root)
    original = MigrationService._copy_essential_dirs

    def write_during_copy(self, snapshot, staging):
        with factory() as session, session.begin():
            session.add(Candidate(display_name="Concurrent import"))
        original(self, snapshot, staging)

    monkeypatch.setattr(MigrationService, "_copy_essential_dirs", write_during_copy)
    target = tmp_path / "migrated"
    result = service.migrate_to(str(target))
    assert result.ok and result.candidate_count == 1
    with closing(sqlite3.connect(target / "db" / "recruit.sqlite3")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM candidate").fetchone() == (1,)
    with factory() as session:
        assert session.query(Candidate).count() == 2
    assert not (target / "db" / "recruit.sqlite3-wal").exists()

