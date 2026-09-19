"""方向异步重判 API：POST /api/backfill/directions 入队 BACKFILL_DIRECTION 并由 worker 执行。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.main import create_app
from kerui_recruit.runtime import build_runtime


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "test"}


@pytest.fixture
def runtime(tmp_path: Path):
    rt = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    yield rt
    rt.services.backup_service.engine.dispose()


def _seed_candidate(runtime, revision_id: str) -> None:
    with runtime.services.session_factory() as session, session.begin():
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1,
                    storage_path=f"x-{revision_id}")
        session.add(blob)
        candidate = Candidate(display_name="测试", status="AVAILABLE")
        session.add(ResumeRevision(
            id=revision_id,
            document=ResumeDocument(candidate=candidate),
            blob=blob,
            content_sha256="a" * 64,
            original_filename=f"{revision_id}.pdf",
            status="READY",
            is_current=True,
            parsed_data={"skills": ["Flink"], "summary": "数据管道开发", "direction": None},
        ))


def _seed_jd(runtime, revision_id: str) -> None:
    with runtime.services.session_factory() as session, session.begin():
        jd = Jd(company="测试公司", title="测试岗位", status="OPEN")
        session.add(JdRevision(
            id=revision_id,
            jd=jd,
            revision_no=1,
            status="READY",
            is_current=True,
            source_text="测试",
            parsed_data={"skills": ["Flink"], "summary": "数据管道开发", "direction": None},
        ))


def test_direction_backfill_endpoint_backfills_candidate(runtime):
    _seed_candidate(runtime, "r-cand-1")
    with TestClient(create_app(runtime.services)) as client:
        response = client.post("/api/backfill/directions", headers=_headers())
        assert response.status_code == 200
        assert response.json()["task_type"] == "BACKFILL_DIRECTION"
    asyncio.run(runtime.worker.run_once())
    with runtime.services.session_factory() as session:
        assert session.get(ResumeRevision, "r-cand-1").parsed_data["direction"] == "BACKEND"


def test_direction_backfill_endpoint_backfills_jd(runtime):
    _seed_jd(runtime, "r-jd-1")
    with TestClient(create_app(runtime.services)) as client:
        response = client.post(
            "/api/backfill/directions",
            json={"entity_type": "jd"},
            headers=_headers(),
        )
        assert response.status_code == 200
        assert response.json()["task_type"] == "BACKFILL_DIRECTION"
    asyncio.run(runtime.worker.run_once())
    with runtime.services.session_factory() as session:
        assert session.get(JdRevision, "r-jd-1").parsed_data["direction"] == "BACKEND"


def test_direction_backfill_endpoint_rejects_invalid_entity_type(runtime):
    with TestClient(create_app(runtime.services)) as client:
        response = client.post(
            "/api/backfill/directions",
            json={"entity_type": "bogus"},
            headers=_headers(),
        )
    assert response.status_code == 422
    assert response.json()["code"] == "E_BACKFILL_INVALID_ARG"
