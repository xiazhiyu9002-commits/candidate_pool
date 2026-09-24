"""Restoring business facts must never leave a newer searchable projection."""
import gc
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, IndexSyncRecord, ResumeDocument, ResumeRevision
from kerui_recruit.main import create_app
from kerui_recruit.runtime import build_runtime
from kerui_recruit.search.sync import enqueue_sync


@pytest.mark.asyncio
async def test_restore_is_staged_until_restart_and_reconciles_search(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data", session_token="test")
    runtime = build_runtime(settings)
    factory = runtime.services.session_factory
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Synthetic", status="AVAILABLE")
        revision = ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="synthetic"),
            content_sha256="a" * 64, original_filename="test.pdf", status="READY", is_current=True,
            parsed_data={"skills": ["Python"]},
        )
        session.add(revision)
        session.flush()
        cid, rid = candidate.id, revision.id
        enqueue_sync(session, "candidate", cid)
    await runtime.services.index_sync_service.run_once()
    snapshot = runtime.services.backup_service.create_snapshot("before_edit")
    with factory() as session, session.begin():
        session.get(ResumeRevision, rid).parsed_data = {"skills": ["Rust"]}
        enqueue_sync(session, "candidate", cid)
    await runtime.services.index_sync_service.run_once()

    with TestClient(create_app(runtime.services)) as client:
        response = client.post(f"/api/backup/restore/{snapshot.name}", headers={"X-Kerui-Session": "test"})
    assert response.status_code == 200
    assert response.json().get("restart_required") is True
    with factory() as session:
        assert session.get(ResumeRevision, rid).parsed_data["skills"] == ["Rust"]
    runtime.services.backup_service.engine.dispose()
    del runtime, client, factory
    gc.collect()

    restored = build_runtime(settings)
    with restored.services.session_factory() as session:
        assert session.get(ResumeRevision, rid).parsed_data["skills"] == ["Python"]
        job = session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == cid))
        assert job.requested_version > job.applied_version
    # Before synchronization there must be no Rust hit from the previous DB.
    assert restored.services.search_service.index.get_revision_chunks(rid) == []
    assert await restored.services.index_sync_service.run_once() == 1
    chunks = restored.services.search_service.index.get_revision_chunks(rid)
    assert "Python" in chunks[0]["keyword_text"]
    assert "Rust" not in chunks[0]["keyword_text"]
    assert list(settings.data_root.glob("search.pre-restore-*"))
    restored.services.backup_service.engine.dispose()


def test_invalid_snapshot_cannot_replace_database_or_create_restore_intent(tmp_path: Path):
    runtime = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    backup = runtime.services.backup_service
    bad = backup.backup_dir / "backup_invalid.sqlite3"
    bad.write_bytes(b"not a sqlite database")
    with runtime.services.session_factory() as session, session.begin():
        session.add(Candidate(display_name="Keep me"))
    with TestClient(create_app(runtime.services)) as client:
        response = client.post(f"/api/backup/restore/{bad.name}", headers={"X-Kerui-Session": "test"})
    assert response.status_code == 422
    with runtime.services.session_factory() as session:
        assert session.scalars(select(Candidate.display_name)).all() == ["Keep me"]
    assert not (backup.backup_dir / "pending-restore.json").exists()
    backup.engine.dispose()


def test_missing_snapshot_returns_structured_not_found(tmp_path: Path):
    """文件名不存在是调用方错误，必须回 404 结构化错误，而不是 500 E_INTERNAL。

    实测（全量接口功能测试）：不带异常映射时会落到全局 Exception 处理器，
    返回 `500 E_INTERNAL` 并把英文内部消息 `Backup not found: …` 透给前端。
    """
    runtime = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    with TestClient(create_app(runtime.services)) as client:
        response = client.post("/api/backup/restore/no-such-snapshot.zip",
                               headers={"X-Kerui-Session": "test"})
    assert response.status_code == 404
    assert response.json()["code"] == "E_BACKUP_NOT_FOUND"
    runtime.services.backup_service.engine.dispose()


def test_snapshot_names_are_unique_even_within_one_second(tmp_path: Path):
    runtime = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    backup = runtime.services.backup_service
    first = backup.create_snapshot("manual")
    second = backup.create_snapshot("manual")
    assert first != second
    assert first.exists() and second.exists()
    backup.engine.dispose()


def test_future_schema_restore_is_rejected_without_poisoning_startup(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data", session_token="test")
    runtime = build_runtime(settings)
    backup = runtime.services.backup_service
    with runtime.services.session_factory() as session, session.begin():
        session.add(Candidate(display_name="Live data"))
    future = backup.create_snapshot("future")
    with sqlite3.connect(future) as connection:
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (999, CURRENT_TIMESTAMP)"
        )
        connection.commit()

    with pytest.raises(ValueError, match="版本"):
        backup.restore_snapshot(future.name)
    assert not (backup.backup_dir / "pending-restore.json").exists()

    # Also protect startup from a corrupt/hand-written pending intent. It must
    # be quarantined rather than replace the live database on every launch.
    staged = backup.backup_dir / "restore-source-deadbeef.sqlite3"
    with sqlite3.connect(future) as source, sqlite3.connect(staged) as target:
        source.backup(target)
    marker = backup.backup_dir / "pending-restore.json"
    marker.write_text(json.dumps({
        "version": 1, "restore_id": "deadbeef", "source": staged.name,
        "requested_from": future.name, "safety_backup": "none",
    }), encoding="utf-8")
    backup.engine.dispose()

    restarted = build_runtime(settings)
    with restarted.services.session_factory() as session:
        assert session.scalars(select(Candidate.display_name)).all() == ["Live data"]
    assert not marker.exists()
    assert list(backup.backup_dir.glob("rejected-restore-*.json"))
    restarted.services.backup_service.engine.dispose()
