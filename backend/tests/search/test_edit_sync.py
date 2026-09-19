from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.services import AppServices
from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.main import create_app
from kerui_recruit.providers.fakes import FakeEmbeddingProvider, FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService
from kerui_recruit.search.sync import IndexSyncService, enqueue_sync
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository


class Embedding:
    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def setup(tmp_path: Path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    service = IndexSyncService(session_factory=factory, index=index, embedding_provider=Embedding())
    yield factory, index, service
    engine.dispose()


@pytest.mark.asyncio
async def test_edit_current_company_is_searchable_after_sync(setup) -> None:
    factory, index, service = setup
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status="AVAILABLE")
        revision = ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="x"),
            content_sha256="a" * 64, original_filename="t.pdf", status="READY", is_current=True,
            parsed_data={"name": "Test", "current_company": "旧公司", "current_title": "工程师",
                         "experiences": [{"company": "旧公司", "title": "工程师"}]},
        )
        session.add(revision)
        session.flush()
        cid = candidate.id
        enqueue_sync(session, "candidate", cid)

    assert await service.run_once() == 1
    assert index.filter_search(CandidateFilters(company="旧公司"), 10)

    # 编辑当前公司，重新入队并同步。
    with factory() as session, session.begin():
        rev = session.query(ResumeRevision).filter(
            ResumeRevision.document.has(candidate_id=cid)).one()
        data = dict(rev.parsed_data)
        data["current_company"] = "新公司"
        rev.parsed_data = data
        enqueue_sync(session, "candidate", cid)

    assert await service.run_once() == 1
    assert index.filter_search(CandidateFilters(company="新公司"), 10)
    # 历史工作经历不被静默篡改：旧公司仍可命中。
    assert index.filter_search(CandidateFilters(company="旧公司"), 10)


@pytest.mark.asyncio
async def test_edit_current_company_is_searchable_immediately(tmp_path: Path) -> None:
    """保存接口同步等待索引完成，返回后立即查询即可命中，无需再手动 run_once。"""
    settings = Settings(data_root=tmp_path / "data", session_token="test-token")
    settings.paths.ensure()
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    embedding = FakeEmbeddingProvider(dimension=2)
    index = LanceDBSearchIndex(settings.paths.search, vector_dimension=2)
    sync = IndexSyncService(session_factory=factory, index=index, embedding_provider=embedding)
    services = AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=BlobStore(settings.paths.blobs, settings.paths.temp),
        task_repository=TaskRepository(factory),
        search_service=HybridSearchService(
            index=index, embedding_provider=embedding, reranker_provider=FakeRerankerProvider()),
        encryption_service=EncryptionService(key_path=str(settings.paths.config / "encryption.key")),
        index_sync_service=sync,
    )
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status="AVAILABLE")
        revision = ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="x"),
            content_sha256="a" * 64, original_filename="t.pdf", status="READY", is_current=True,
            parsed_data={"name": "Test", "current_company": "旧公司",
                         "experiences": [{"company": "旧公司", "title": "工程师"}]},
        )
        session.add(revision)
        session.flush()
        cid = candidate.id
        enqueue_sync(session, "candidate", cid)
    await sync.run_once()

    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.put(
            f"/api/resumes/candidate/{cid}/field",
            json={"field": "current_company", "value": "新公司"},
            headers={"X-Kerui-Session": "test-token"},
        )
        assert response.status_code == 200, response.text

    # 接口返回后立即查询（不手动 run_once）即可命中新公司。
    assert index.filter_search(CandidateFilters(company="新公司"), 10)
    assert index.filter_search(CandidateFilters(company="旧公司"), 10)
