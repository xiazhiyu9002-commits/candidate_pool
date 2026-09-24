"""人工改过年龄后，年龄基准必须重置，否则下一次日滚动会按旧基准把人工值覆盖回去。"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.runtime import create_runtime_app

HEADERS = {"X-Kerui-Session": "test"}


def _app(tmp_path):
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test"))
    )
    return app, TestClient(app)


def _seed_candidate(app, *, age: int = 27) -> str:
    with app.state.services.session_factory() as session, session.begin():
        candidate = Candidate(display_name="张伟", status="AVAILABLE")
        sha = uuid4().hex
        session.add(ResumeRevision(
            document=ResumeDocument(candidate=candidate),
            blob=Blob(content_sha256=sha, suffix=".pdf", size_bytes=1, storage_path=f"x-{sha}"),
            content_sha256=sha,
            original_filename="x.pdf",
            status="READY",
            is_current=True,
            parsed_data={"name": "张伟", "age": age, "total_years": "10.0", "experiences": []},
        ))
        session.flush()
        return candidate.id


def test_editing_age_resets_the_rollover_baseline(tmp_path) -> None:
    app, client = _app(tmp_path)
    candidate_id = _seed_candidate(app, age=27)

    response = client.put(
        f"/api/resumes/candidate/{candidate_id}/parsed",
        headers=HEADERS,
        json={"parsed_data": {"age": 25}},
    )
    assert response.status_code == 200

    with app.state.services.session_factory() as session:
        revision = session.scalar(select(ResumeRevision))
        parsed = revision.parsed_data
        assert parsed["age"] == 25
        assert parsed["age_baseline"] == 25
        assert parsed["age_baseline_year"] == datetime.now().year
