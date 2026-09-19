from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Candidate, Jd, JdRevision
from kerui_recruit.runtime import create_runtime_app


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("launch-token"),
    )
    return create_runtime_app(settings)


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "launch-token"}


def _seed_jd(session, company: str, title: str, status: str = "OPEN") -> str:
    jd = Jd(company=company, title=title, status=status)
    jd.revisions.append(JdRevision(revision_no=1, status="READY", is_current=True, source_text="Java"))
    session.add(jd)
    session.flush()
    return jd.id


def test_case_list_pagination_and_filter(app, tmp_path: Path) -> None:
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session, session.begin():
            candidates = [Candidate(display_name=f"候选人{i}") for i in range(25)]
            session.add_all(candidates)
            session.flush()
            candidate_ids = [c.id for c in candidates]
            jd_id = _seed_jd(session, "某公司", "Java 后端")

        for cid in candidate_ids:
            created = client.post(
                "/api/case",
                json={"candidate_id": cid, "jd_id": jd_id},
                headers=_headers(),
            )
            assert created.status_code == 200

        first = client.get("/api/case", params={"page": 1, "page_size": 20}, headers=_headers())
        assert first.status_code == 200
        body = first.json()
        assert body["total"] == 25
        assert len(body["items"]) == 20
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert body["has_more"] is True

        second = client.get("/api/case", params={"page": 2, "page_size": 20}, headers=_headers())
        assert second.status_code == 200
        assert len(second.json()["items"]) == 5
        assert second.json()["has_more"] is False

        filtered = client.get("/api/case", params={"page": 1, "page_size": 20, "jd_id": jd_id}, headers=_headers())
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 25


def test_jd_list_pagination_and_filters(app, tmp_path: Path) -> None:
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session, session.begin():
            for i in range(12):
                _seed_jd(session, f"公司{i}", f"Java 岗位{i}")

        first = client.get("/api/jd/page", params={"page": 1, "page_size": 10}, headers=_headers())
        assert first.status_code == 200
        body = first.json()
        assert body["total"] == 12
        assert len(body["items"]) == 10
        assert body["has_more"] is True

        second = client.get("/api/jd/page", params={"page": 2, "page_size": 10}, headers=_headers())
        assert second.status_code == 200
        assert len(second.json()["items"]) == 2
        assert second.json()["has_more"] is False

        title = client.get("/api/jd/page", params={"page": 1, "page_size": 10, "title": "岗位5"}, headers=_headers())
        assert title.status_code == 200
        assert title.json()["total"] == 1
        assert title.json()["items"][0]["title"] == "Java 岗位5"

        company = client.get("/api/jd/page", params={"page": 1, "page_size": 10, "company": "公司3"}, headers=_headers())
        assert company.status_code == 200
        assert company.json()["total"] == 1
        assert company.json()["items"][0]["company"] == "公司3"
