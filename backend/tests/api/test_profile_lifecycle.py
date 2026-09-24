from dataclasses import replace
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
async def test_profile_stream_reports_stages_then_result(tmp_path: Path) -> None:
    """候选人画像的流式重生成：先推阶段（loading/draft），最后推结果。

    与岗位侧同一个 `api/profile_stream.py`，但走的是另一个端点的接线，必须分别证明。
    """
    services = build_services(tmp_path)
    candidate_id, _ = seed_candidate_with_profile(services)

    class _StreamingBackfill:
        async def regenerate_candidate_profile(self, candidate_id: str, instruction=None, *, on_stage=None):
            assert on_stage is not None, "流式路径必须把阶段回调传下去"
            on_stage("loading")
            on_stage("draft")
            return {"generated": True, "summary": "五年后端经验。",
                    "points": [{"text": "五年后端经验。", "evidence_paths": []}],
                    "compact": "五年后端经验。"}

    services = replace(services, backfill_service=_StreamingBackfill())
    transport = httpx.ASGITransport(app=create_app(services), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            f"/api/resumes/candidate/{candidate_id}/regen-profile",
            json={"instruction": ""},
            headers={"X-Kerui-Session": "test-token", "Accept": "text/event-stream"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert '"stage": "loading"' in body and '"stage": "draft"' in body, body
    assert "event: result" in body
    assert '"summary": "五年后端经验。"' in body


@pytest.mark.asyncio
async def test_profile_stream_without_accept_header_stays_json(tmp_path: Path) -> None:
    """没要求流式时仍是普通 JSON（内容协商，不是把同步路径改掉）。

    接口探针与既有调用方都不带 `Accept: text/event-stream`，它们的响应必须一字不变。
    """
    services = build_services(tmp_path)
    candidate_id, _ = seed_candidate_with_profile(services)

    class _Backfill:
        async def regenerate_candidate_profile(self, candidate_id: str, instruction=None, *, on_stage=None):
            return {"generated": True, "summary": "五年后端经验。", "points": [], "compact": "五年后端经验。"}

    services = replace(services, backfill_service=_Backfill())
    transport = httpx.ASGITransport(app=create_app(services), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            f"/api/resumes/candidate/{candidate_id}/regen-profile",
            json={"instruction": ""},
            headers={"X-Kerui-Session": "test-token"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["summary"] == "五年后端经验。"


@pytest.mark.asyncio
async def test_profile_regeneration_surfaces_provider_unavailable_as_502(tmp_path: Path) -> None:
    """画像重生成遇到「没有可用的 AI 服务」要回 502/503，不能压成 500 内部错误。

    实测（全量接口功能测试）：`E_AI_NO_PROVIDER` 被 `except Exception` 包成
    `500 E_PROFILE_GENERATION_FAILED`，前端无法区分「服务不可用可重试」与「真出错了」。
    岗位侧 `/api/jd/{id}/regen-profile` 同一个坑，见 test_jd_profile_regeneration_error_mapping。
    """
    from kerui_recruit.providers.errors import ProviderError

    services = build_services(tmp_path)
    candidate_id, _ = seed_candidate_with_profile(services)

    class _UnavailableBackfill:
        async def regenerate_candidate_profile(self, candidate_id: str, instruction=None, *, on_stage=None):
            raise ProviderError(code="E_AI_NO_PROVIDER", retryable=True,
                                user_message="没有可用的 AI 服务")

    # AppServices 是 frozen dataclass：换服务要用 replace 造一份，不能就地赋值。
    services = replace(services, backfill_service=_UnavailableBackfill())
    transport = httpx.ASGITransport(app=create_app(services), raise_app_exceptions=False)
    headers = {"X-Kerui-Session": "test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        response = await client.post(
            f"/api/resumes/candidate/{candidate_id}/regen-profile",
            json={"instruction": "一句话概括"},
            headers=headers,
        )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "E_AI_NO_PROVIDER"


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
