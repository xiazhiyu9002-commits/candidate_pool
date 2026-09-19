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
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository


def build_services(tmp_path: Path) -> AppServices:
    settings = Settings(data_root=tmp_path / "data", session_token="test-token")
    settings.paths.ensure()
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    embedding = FakeEmbeddingProvider(dimension=16)
    index = LanceDBSearchIndex(settings.paths.search, vector_dimension=16)
    return AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=BlobStore(settings.paths.blobs, settings.paths.temp),
        task_repository=TaskRepository(factory),
        search_service=HybridSearchService(
            index=index,
            embedding_provider=embedding,
            reranker_provider=FakeRerankerProvider(),
        ),
        encryption_service=EncryptionService(key_path=str(settings.paths.config / "encryption.key")),
    )


def seed_candidate_with_profile(services: AppServices) -> tuple[str, str]:
    with services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="张三")
        blob = Blob(content_sha256="a" * 64, suffix=".txt", size_bytes=1, storage_path="blob")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(
            document=document, blob=blob, content_sha256="a" * 64,
            original_filename="a.txt", status="READY", is_current=True,
            parsed_data={
                "name": "张三", "skills": ["Python"],
                "ai_profile_summary": "AI 画像", "ai_profile_source": "ai",
                "ai_profile_input_hash": "hash", "ai_profile_stale": False,
            },
        )
        session.add_all([candidate, blob, document, revision])
        session.flush()
        return candidate.id, revision.id


@pytest.mark.asyncio
async def test_editing_profile_input_marks_ai_profile_stale(tmp_path: Path) -> None:
    """修改参与画像生成的输入字段后，AI 画像应标记为 stale=true。"""
    services = build_services(tmp_path)
    candidate_id, revision_id = seed_candidate_with_profile(services)
    transport = httpx.ASGITransport(app=create_app(services))
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.put(
            f"/api/resumes/candidate/{candidate_id}/field",
            json={"field": "skills", "value": ["Java"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.parsed_data["ai_profile_stale"] is True
        assert revision.parsed_data["ai_profile_source"] == "ai"


@pytest.mark.asyncio
async def test_editing_profile_summary_marks_manual_and_not_stale(tmp_path: Path) -> None:
    """人工编辑画像正文后 source=manual、stale=false。"""
    services = build_services(tmp_path)
    candidate_id, revision_id = seed_candidate_with_profile(services)
    transport = httpx.ASGITransport(app=create_app(services))
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.put(
            f"/api/resumes/candidate/{candidate_id}/field",
            json={"field": "ai_profile_summary", "value": "人工画像"},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    with services.session_factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        assert revision.parsed_data["ai_profile_summary"] == "人工画像"
        assert revision.parsed_data["ai_profile_source"] == "manual"
        assert revision.parsed_data["ai_profile_input_hash"] is None
        assert revision.parsed_data["ai_profile_stale"] is False
