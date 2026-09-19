import asyncio
import threading
from pathlib import Path

import httpx
import pymupdf
import pytest

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import ResumeRevision, TaskRecord
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.runtime import (
    _lease_recovery_loop,
    _worker_loop,
    build_runtime,
    create_runtime_app,
)
from kerui_recruit.search.contracts import CandidateFilters
from sqlalchemy import select


def make_resume_pdf() -> bytes:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Zhang San Python Finance 6 years Master Shanghai")
    content = document.tobytes()
    document.close()
    return content


@pytest.mark.asyncio
async def test_runtime_processes_an_import_without_external_services(tmp_path: Path) -> None:
    """Removing the packaged worker or local providers must break the offline first-run flow."""
    settings = Settings(data_root=tmp_path / "data", session_token="launch-token")
    runtime = build_runtime(settings)
    with runtime.services.session_factory() as session:
        imported = ResumeIngestService(session, runtime.services.blob_store).ingest(
            IngestResume(filename="张三.pdf", content=make_resume_pdf())
        )

    assert await runtime.worker.run_once() is True

    with runtime.services.session_factory() as session:
        assert session.get(TaskRecord, imported.task_id).status == "SUCCESS"
        assert session.get(ResumeRevision, imported.revision_id).status == "READY"
    page = await runtime.services.search_service.search(
        "Python", CandidateFilters(), limit=20
    )
    assert page.items[0].candidate_id == imported.candidate_id


@pytest.mark.asyncio
async def test_runtime_app_reports_readiness_after_local_stores_open(tmp_path: Path) -> None:
    """The desktop shell must not show the UI before the embedded stores are ready."""
    settings = Settings(data_root=tmp_path / "data", session_token="launch-token")
    app = create_runtime_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_busy_local_worker_yields_to_api_requests() -> None:
    class BusyWorker:
        def __init__(self) -> None:
            self.calls = 0
            self.block = asyncio.Event()

        async def run_once(self) -> bool:
            self.calls += 1
            if self.calls > 10:
                await self.block.wait()
            return True

    worker = BusyWorker()
    task = asyncio.create_task(_worker_loop(worker))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    observed_calls = worker.calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed_calls == 1


@pytest.mark.asyncio
async def test_runtime_periodically_recovers_expired_task_leases() -> None:
    class Repository:
        def __init__(self) -> None:
            self.called = threading.Event()

        def recover_expired_leases(self) -> int:
            self.called.set()
            return 1

    repository = Repository()
    task = asyncio.create_task(
        _lease_recovery_loop(repository, interval_seconds=0.01)  # type: ignore[arg-type]
    )
    assert await asyncio.to_thread(repository.called.wait, 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_passive_match_failure_is_retryable_without_reparsing(tmp_path, monkeypatch):
    runtime = build_runtime(Settings(data_root=tmp_path / 'data', session_token='test'))
    with runtime.services.session_factory() as session:
        imported = ResumeIngestService(session, runtime.services.blob_store).ingest(
            IngestResume(filename='sample.pdf', content=make_resume_pdf()))
    async def unavailable(*args, **kwargs):
        raise RuntimeError('controlled match outage')
    monkeypatch.setattr(runtime.services.match_service, 'reverse_match_candidate', unavailable)
    assert await runtime.worker.run_once()
    with runtime.services.session_factory() as session:
        assert session.get(TaskRecord, imported.task_id).status == 'SUCCESS'
        task = session.scalar(select(TaskRecord).where(TaskRecord.task_type == 'MATCH_CANDIDATE'))
        assert task is not None and task.status == 'QUEUED'
        match_task_id = task.id
    assert await runtime.worker.run_once()
    with runtime.services.session_factory() as session:
        assert session.get(TaskRecord, match_task_id).status == 'RETRY_WAIT'
        assert session.get(ResumeRevision, imported.revision_id).status == 'READY'


@pytest.mark.asyncio
async def test_embedding_outage_does_not_turn_successful_parsing_into_failure(tmp_path, monkeypatch):
    runtime = build_runtime(Settings(data_root=tmp_path / 'data', session_token='test'))
    with runtime.services.session_factory() as session:
        imported = ResumeIngestService(session, runtime.services.blob_store).ingest(
            IngestResume(filename='sample.pdf', content=make_resume_pdf()))
    async def unavailable(*args, **kwargs):
        raise RuntimeError('controlled embedding outage')
    monkeypatch.setattr(runtime.providers.embedding, 'embed_documents', unavailable)
    assert await runtime.worker.run_once()
    with runtime.services.session_factory() as session:
        assert session.get(TaskRecord, imported.task_id).status == 'SUCCESS'
        assert session.get(ResumeRevision, imported.revision_id).status == 'READY'
    assert runtime.services.index_sync_service.status()['failed'] == 1


def test_runtime_reconciles_legacy_onboarding_projection_before_serving(tmp_path):
    from kerui_recruit.cases.service import CaseService
    from kerui_recruit.db.models import Candidate, IndexSyncRecord, Jd

    settings = Settings(data_root=tmp_path / "data", session_token="test")
    first = build_runtime(settings)
    with first.services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="Legacy", status="AVAILABLE")
        job = Jd(company="Legacy Co", title="Role", status="OPEN")
        session.add_all([candidate, job])
        session.flush()
        candidate_id, job_id = candidate.id, job.id
    cases = CaseService(first.services.session_factory)
    case = cases.create(candidate_id=candidate_id, jd_id=job_id)
    cases.onboard(case.id)
    with first.services.session_factory() as session, session.begin():
        candidate = session.get(Candidate, candidate_id)
        candidate.status = "AVAILABLE"
        candidate.workflow_previous_status = None
        session.query(IndexSyncRecord).filter_by(entity_type="candidate", entity_id=candidate_id).delete()
    first.services.backup_service.engine.dispose()

    restarted = build_runtime(settings)
    with restarted.services.session_factory() as session:
        candidate = session.get(Candidate, candidate_id)
        assert candidate.status == "ON_HOLD"
        assert candidate.workflow_previous_status == "AVAILABLE"
        sync = session.scalar(select(IndexSyncRecord).where(
            IndexSyncRecord.entity_type == "candidate", IndexSyncRecord.entity_id == candidate_id))
        assert sync is not None and sync.requested_version > sync.applied_version
    restarted.services.backup_service.engine.dispose()


def test_runtime_routes_lead_extractor_through_text_capability(tmp_path: Path) -> None:
    """切走 DeepSeek、仅配置自定义文本供应商时，BD 线索提取不应崩溃，并复用该文本能力。"""
    settings = Settings(
        data_root=tmp_path / "data",
        session_token="test",
        text_api_key="custom-text-key",
        text_base_url="https://custom.example/v1",
        text_model="custom-model",
    )
    runtime = build_runtime(settings)
    try:
        extractor = runtime.services.bd_search_service.extractor
        assert extractor is not None
        # 迁移后 text_api_key 应成为 custom_openai 主连接，线索提取通过该路由而非直接 HTTP。
        manager = runtime.services.ai_manager
        assert manager is not None
        assert [c.provider_id for c in manager.config.connections] == ["custom_openai"]
    finally:
        runtime.services.backup_service.engine.dispose()


def test_plan_index_upgrade_separates_inplace_from_reset(tmp_path):
    import json

    from kerui_recruit.providers.factory import embedding_identity
    from kerui_recruit.search.lancedb_index import INDEX_CHUNK_VERSION, INDEX_SCHEMA_VERSION
    from kerui_recruit.search.upgrade import INPLACE, RESET, plan_index_upgrade

    settings = Settings(data_root=tmp_path / "data", session_token="test")
    settings.paths.ensure()
    search = settings.paths.search
    search.mkdir(parents=True, exist_ok=True)
    model, dimension = embedding_identity(settings)

    def plan():
        return plan_index_upgrade(search, embedding_model=model, vector_dimension=dimension)

    # 无 metadata 文件（全新安装/空索引）：不触发重建。
    assert plan() is None

    metadata_path = search / "candidate-index-metadata.json"

    # 只落后 schema/chunk 版本（模型与维度未变）：就地补列即可，不丢旧数据。
    metadata_path.write_text(
        json.dumps({"schema_version": "1", "embedding_model": model,
                    "vector_dimension": dimension, "chunk_version": "1"}),
        encoding="utf-8",
    )
    assert plan().mode == INPLACE

    # 向量维度变了：新旧行无法共存，只能归档重建。
    metadata_path.write_text(
        json.dumps({"schema_version": "1", "embedding_model": model,
                    "vector_dimension": dimension * 2, "chunk_version": "1"}),
        encoding="utf-8",
    )
    assert plan().mode == RESET

    # embedding 模型换了（维度相同）：旧行既读不出也写不回，同样归档重建。
    metadata_path.write_text(
        json.dumps({"schema_version": "1", "embedding_model": "other-model",
                    "vector_dimension": dimension, "chunk_version": "1"}),
        encoding="utf-8",
    )
    assert plan().mode == RESET

    # 匹配当前版本的 metadata：不触发重建。
    metadata_path.write_text(
        json.dumps({"schema_version": INDEX_SCHEMA_VERSION, "embedding_model": model,
                    "vector_dimension": dimension, "chunk_version": INDEX_CHUNK_VERSION}),
        encoding="utf-8",
    )
    assert plan() is None


def test_runtime_repairs_incompatible_index_in_place(tmp_path):
    import json

    from kerui_recruit.db.models import Candidate, IndexSyncRecord
    from kerui_recruit.search.lancedb_index import INDEX_SCHEMA_VERSION
    from kerui_recruit.search.upgrade import INPLACE

    settings = Settings(data_root=tmp_path / "data", session_token="test")
    first = build_runtime(settings)
    with first.services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="张三", status="AVAILABLE")
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    first.services.backup_service.engine.dispose()

    # 旧索引：模型与维度都没变，只有 schema/chunk 版本落后（可物理共存）。
    search = settings.paths.search
    search.mkdir(parents=True, exist_ok=True)
    metadata_path = search / "candidate-index-metadata.json"
    metadata_path.write_text(
        json.dumps({"schema_version": "1", "embedding_model": "local-hash-v1",
                    "vector_dimension": 64, "chunk_version": "1"}),
        encoding="utf-8",
    )

    second = build_runtime(settings)
    with second.services.session_factory() as session:
        sync = session.scalar(select(IndexSyncRecord).where(
            IndexSyncRecord.entity_type == "candidate",
            IndexSyncRecord.entity_id == candidate_id,
        ))
        # 版本提升后全量重投一次，让索引口径整体收敛。
        assert sync is not None
    second.services.backup_service.engine.dispose()

    # 就地修复：旧索引目录原地保留、没有归档，metadata 刷新为当前版本。
    assert metadata_path.is_file()
    assert list(settings.paths.search.parent.glob("search.pre-rebuild-*")) == []
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["schema_version"] == INDEX_SCHEMA_VERSION

    # 重建状态已落盘，界面据此提示用户。
    state = json.loads((settings.paths.root / "index-rebuild.json").read_text(encoding="utf-8"))
    assert state["mode"] == INPLACE
    assert state["finished_at"] is None


def test_runtime_archives_index_when_vector_dimension_changes(tmp_path):
    import json

    from kerui_recruit.db.models import Candidate, IndexSyncRecord
    from kerui_recruit.search.upgrade import RESET

    settings = Settings(data_root=tmp_path / "data", session_token="test")
    first = build_runtime(settings)
    with first.services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="张三", status="AVAILABLE")
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    first.services.backup_service.engine.dispose()

    # 旧索引的向量长度与当前代码不一致：新旧行无法共存，只能归档重建。
    search = settings.paths.search
    search.mkdir(parents=True, exist_ok=True)
    (search / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "9", "embedding_model": "local-hash-v1",
                    "vector_dimension": 1024, "chunk_version": "7"}),
        encoding="utf-8",
    )

    second = build_runtime(settings)
    with second.services.session_factory() as session:
        sync = session.scalar(select(IndexSyncRecord).where(
            IndexSyncRecord.entity_type == "candidate",
            IndexSyncRecord.entity_id == candidate_id,
        ))
        assert sync is not None
    second.services.backup_service.engine.dispose()

    archived = list(settings.paths.search.parent.glob("search.pre-rebuild-*"))
    assert len(archived) == 1
    assert (archived[0] / "candidate-index-metadata.json").is_file()

    state = json.loads((settings.paths.root / "index-rebuild.json").read_text(encoding="utf-8"))
    assert state["mode"] == RESET
    assert Path(state["archived"]).name == archived[0].name


def test_runtime_explicit_rebuild_archives_old_index(tmp_path):
    import json

    from kerui_recruit.backup.snapshot import REBUILD_MARKER

    settings = Settings(data_root=tmp_path / "data", session_token="test")
    first = build_runtime(settings)
    first.services.backup_service.engine.dispose()

    # 先造出一个“旧索引”（含 metadata 文件），再放置显式重建标记。
    search = settings.paths.search
    search.mkdir(parents=True, exist_ok=True)
    metadata_path = search / "candidate-index-metadata.json"
    metadata_path.write_text(
        json.dumps({"schema_version": "8", "embedding_model": "local-hash-v1",
                    "vector_dimension": 64, "chunk_version": "6"}),
        encoding="utf-8",
    )
    (settings.paths.root / REBUILD_MARKER).write_text("1\n", encoding="ascii")

    second = build_runtime(settings)
    second.services.backup_service.engine.dispose()

    # 显式重建：旧索引被归档（而非删除），并生成新的空 search 目录。
    archived = list(settings.paths.search.parent.glob("search.pre-rebuild-*"))
    assert len(archived) == 1
    assert (archived[0] / "candidate-index-metadata.json").is_file()
    # 重建标记被消费。
    assert not (settings.paths.root / REBUILD_MARKER).exists()
