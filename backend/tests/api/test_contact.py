from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.services import AppServices
from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CandidateContact
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.main import create_app
from kerui_recruit.providers.fakes import FakeEmbeddingProvider, FakeRerankerProvider
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository


def build_services(tmp_path: Path) -> tuple[AppServices, EncryptionService]:
    settings = Settings(data_root=tmp_path / "data", session_token="test-token")
    settings.paths.ensure()
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    encryption = EncryptionService(key_path=str(settings.paths.config / "encryption.key"))
    embedding = FakeEmbeddingProvider(dimension=16)
    index = LanceDBSearchIndex(settings.paths.search, vector_dimension=16)
    services = AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=BlobStore(settings.paths.blobs, settings.paths.temp),
        task_repository=TaskRepository(factory),
        search_service=HybridSearchService(
            index=index,
            embedding_provider=embedding,
            reranker_provider=FakeRerankerProvider(),
        ),
        encryption_service=encryption,
    )
    return services, encryption


def seed_candidate(services: AppServices) -> str:
    with services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.flush()
        return candidate.id


@pytest.mark.asyncio
async def test_contact_get_returns_empty_when_absent(tmp_path: Path) -> None:
    services, _ = build_services(tmp_path)
    candidate_id = seed_candidate(services)
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.get(
            f"/api/resumes/candidate/{candidate_id}/contact",
            headers={"X-Kerui-Session": "test-token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] is None
    assert body["phone"] is None


@pytest.mark.asyncio
async def test_contact_round_trip_encrypts_at_rest(tmp_path: Path) -> None:
    services, encryption = build_services(tmp_path)
    candidate_id = seed_candidate(services)
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        headers = {"X-Kerui-Session": "test-token"}
        updated = await client.put(
            f"/api/resumes/candidate/{candidate_id}/contact",
            json={"email": "zhang@example.com", "phone": "13800138000"},
            headers=headers,
        )
        fetched = await client.get(
            f"/api/resumes/candidate/{candidate_id}/contact",
            headers=headers,
        )

    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["email"] == "zhang@example.com"
    assert fetched.json()["phone"] == "13800138000"

    # Verify the stored values are encrypted, not plain text.
    with services.session_factory() as session:
        contact = session.scalar(
            select(CandidateContact).where(CandidateContact.candidate_id == candidate_id)
        )
        assert contact is not None
        assert contact.email_encrypted != "zhang@example.com"
        assert contact.phone_encrypted != "13800138000"
        assert encryption.decrypt(contact.email_encrypted) == "zhang@example.com"
        assert encryption.decrypt(contact.phone_encrypted) == "13800138000"
        assert contact.email_fingerprint == "zhang@example.com"
        assert contact.phone_fingerprint == "13800138000"


@pytest.mark.asyncio
async def test_contact_update_unknown_candidate_returns_404(tmp_path: Path) -> None:
    services, _ = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.put(
            "/api/resumes/candidate/missing/contact",
            json={"email": "a@b.com", "phone": "13800138000"},
            headers={"X-Kerui-Session": "test-token"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "E_CANDIDATE_NOT_FOUND"
