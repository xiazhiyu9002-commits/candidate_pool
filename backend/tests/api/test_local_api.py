from pathlib import Path

import httpx
import pymupdf
import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.services import AppServices
from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.main import create_app
from kerui_recruit.providers.fakes import FakeEmbeddingProvider, FakeRerankerProvider
from kerui_recruit.resumes.pipeline import ResumePipeline
from kerui_recruit.resumes.structured import ParsedExperience, ParsedResume
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService
from kerui_recruit.search.sync import IndexSyncService
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository


class FixedResumeParser:
    async def parse_resume(self, text: str) -> ParsedResume:
        assert "Python Finance" in text
        return ParsedResume(
            name="张三",
            total_years=6,
            highest_degree="硕士",
            location="上海",
            skills=["Python", "金融风控"],
            summary="金融科技后端工程师",
            experiences=[
                ParsedExperience(
                    company="示例科技",
                    title="后端工程师",
                    summary="Python 金融风控",
                )
            ],
        )


def make_pdf_bytes() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Python Finance Resume")
    content = pdf.tobytes()
    pdf.close()
    return content


def build_services(tmp_path: Path) -> tuple[AppServices, ResumePipeline]:
    settings = Settings(data_root=tmp_path / "data", session_token="test-token")
    settings.paths.ensure()
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    blob_store = BlobStore(settings.paths.blobs, settings.paths.temp)
    embedding = FakeEmbeddingProvider(dimension=16)
    index = LanceDBSearchIndex(settings.paths.search, vector_dimension=16)
    services = AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=blob_store,
        task_repository=TaskRepository(factory),
        index_sync_service=IndexSyncService(session_factory=factory, index=index, embedding_provider=embedding),
        search_service=HybridSearchService(
            index=index,
            embedding_provider=embedding,
            reranker_provider=FakeRerankerProvider(),
        ),
    )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=blob_store,
        parser=FixedResumeParser(),
        embedding_provider=embedding,
        search_index=index,
    )
    return services, pipeline


@pytest.mark.asyncio
async def test_api_rejects_missing_local_session_token(tmp_path: Path) -> None:
    """Another local process must not be able to read candidate data without the launch token."""
    services, _ = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            "/api/search/candidates",
            json={"query": "Python", "filters": {}, "limit": 20},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "E_LOCAL_SESSION"
    assert "request_id" in response.json()


@pytest.mark.asyncio
async def test_resume_import_reaches_task_status_and_search(tmp_path: Path) -> None:
    """The public local API must complete the first real user workflow end to end."""
    services, pipeline = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        imported = await client.post(
            "/api/resumes/import",
            files={"file": ("张三.pdf", make_pdf_bytes(), "application/pdf")},
            headers=headers,
        )
        assert imported.status_code == 202
        payload = imported.json()
        task = await client.get(f"/api/tasks/{payload['task_id']}", headers=headers)
        assert task.status_code == 200
        assert task.json()["status"] == "PENDING"

        await pipeline.run(payload["revision_id"])
        assert await services.index_sync_service.run_once(force=True) == 1
        searched = await client.post(
            "/api/search/candidates",
            json={
                "query": "Python 金融",
                "filters": {"min_years": 5, "highest_degree": "MASTER", "location": "上海"},
                "limit": 20,
            },
            headers=headers,
        )

    assert searched.status_code == 200
    result = searched.json()
    assert result["items"][0]["candidate_id"] == payload["candidate_id"]
    assert result["degraded_reasons"] == []


@pytest.mark.asyncio
async def test_unsupported_upload_returns_stable_chinese_error(tmp_path: Path) -> None:
    """A user-facing validation failure must never expose a Python stack trace."""
    services, _ = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            "/api/resumes/import",
            files={"file": ("payload.exe", b"bad", "application/octet-stream")},
            headers={"X-Kerui-Session": "test-token"},
        )

    assert response.status_code == 415
    assert response.json()["code"] == "E_FILE_TYPE_UNSUPPORTED"
    assert response.json()["message"] == "仅支持 PDF、DOC 和 DOCX 简历"
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_reparse_endpoint_is_idempotent(tmp_path: Path) -> None:
    """重复点击重新解析不应创建重复任务，且 payload 显式携带 use_vision。"""
    services, _ = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        imported = await client.post(
            "/api/resumes/import",
            files={"file": ("张三.pdf", make_pdf_bytes(), "application/pdf")},
            headers=headers,
        )
        revision_id = imported.json()["revision_id"]

        first = await client.post(
            f"/api/resumes/revisions/{revision_id}/reparse",
            json={"use_vision": True},
            headers=headers,
        )
        second = await client.post(
            f"/api/resumes/revisions/{revision_id}/reparse",
            json={"use_vision": True},
            headers=headers,
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    task = services.task_repository.list()
    reparse_tasks = [
        entry for entry in task
        if entry.idempotency_key == f"REPARSE_RESUME:{revision_id}:vision"
    ]
    assert len(reparse_tasks) == 1
    assert reparse_tasks[0].payload["use_vision"] is True


@pytest.mark.asyncio
async def test_search_accepts_new_filter_fields_without_type_error(tmp_path: Path) -> None:
    """新过滤字段（多地点/意向地/排除技能）贯通请求模型，不产生重复传参 TypeError。"""
    services, _ = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            "/api/search/candidates",
            json={
                "query": "Python",
                "filters": {
                    "locations": ["上海", "北京"],
                    "preferred_locations": ["深圳"],
                    "exclude_skills": ["Java"],
                    "min_years": 3,
                    "highest_degree": "BACHELOR",
                },
                "limit": 20,
            },
            headers=headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "degraded_reasons" in body
