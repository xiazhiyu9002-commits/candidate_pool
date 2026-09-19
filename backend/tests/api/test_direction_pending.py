"""方向待核队列 API：GET /api/resumes/candidates/direction-pending 只返回待核候选人。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.main import create_app
from kerui_recruit.runtime import build_runtime


@pytest.fixture
def runtime(tmp_path: Path):
    rt = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    yield rt
    rt.services.backup_service.engine.dispose()


def _seed(runtime, revision_id: str, direction) -> None:
    sha = hashlib.sha256(revision_id.encode()).hexdigest()
    with runtime.services.session_factory() as session, session.begin():
        blob = Blob(content_sha256=sha, suffix=".pdf", size_bytes=1,
                    storage_path=f"x-{revision_id}")
        session.add(blob)
        candidate = Candidate(display_name=f"cand-{revision_id}", status="AVAILABLE")
        session.add(ResumeRevision(
            id=revision_id,
            document=ResumeDocument(candidate=candidate),
            blob=blob,
            content_sha256=sha,
            original_filename=f"{revision_id}.pdf",
            status="READY",
            is_current=True,
            parsed_data={"skills": ["Java"], "summary": "后端", "direction": direction},
        ))


def test_direction_pending_lists_only_pending(runtime):
    _seed(runtime, "r-null", None)
    _seed(runtime, "r-other", "OTHER")
    _seed(runtime, "r-invalid", "OPERATIONS")
    _seed(runtime, "r-ok", "BACKEND")
    with TestClient(create_app(runtime.services)) as client:
        response = client.get(
            "/api/resumes/candidates/direction-pending",
            headers={"X-Kerui-Session": "test"},
        )
    assert response.status_code == 200
    revision_ids = {item["revision_id"] for item in response.json()}
    assert "r-ok" not in revision_ids
    assert {"r-null", "r-other", "r-invalid"} <= revision_ids
