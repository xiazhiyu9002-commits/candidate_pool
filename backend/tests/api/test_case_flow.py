from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Candidate
from kerui_recruit.runtime import create_runtime_app


@pytest.mark.parametrize('path', ['/overview', '/by-jd', '/trend', '/export'])
def test_dashboard_reversed_dates_are_validation_errors(app, path):
    response = TestClient(app, raise_server_exceptions=False).get('/api/dashboard' + path,
        params={'date_from': '2026-09-02', 'date_to': '2026-09-01', 'granularity': 'week'}, headers=_headers())
    assert response.status_code == 422
    assert response.json()['code'] == 'E_DASHBOARD_FILTER'


def test_dashboard_unknown_granularity_is_validation_error(app):
    response = TestClient(app, raise_server_exceptions=False).get('/api/dashboard/trend',
        params={'granularity': 'year'}, headers=_headers())
    assert response.status_code == 422


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("launch-token"),
    )
    return create_runtime_app(settings)


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "launch-token"}


def _seed_jd(client: TestClient) -> str:
    imported = client.post(
        "/api/jd/import",
        json={"company": "某公司", "title": "Java 后端", "source_text": "Java 3年 本科"},
        headers=_headers(),
    )
    return imported.json()["jd_id"]


def test_case_full_pipeline_via_api(app, tmp_path: Path) -> None:
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session:
            candidate = Candidate(display_name="张三")
            session.add(candidate)
            session.commit()
            candidate_id = candidate.id

        jd_id = _seed_jd(client)

        created = client.post(
            "/api/case",
            json={"candidate_id": candidate_id, "jd_id": jd_id},
            headers=_headers(),
        )
        assert created.status_code == 200
        case_id = created.json()["id"]
        assert created.json()["stage"] == "待评估"

        rec = client.post(
            f"/api/case/{case_id}/recommend",
            json={"note": "推荐给客户"},
            headers=_headers(),
        )
        assert rec.status_code == 200
        assert rec.json()["event_type"] == "RECOMMENDED"

        entered = client.post(
            f"/api/case/{case_id}/enter-interview",
            json={},
            headers=_headers(),
        )
        assert entered.status_code == 200
        round_id = entered.json()["case_round_id"]

        passed = client.post(
            f"/api/case/{case_id}/pass-and-advance",
            json={"case_round_id": round_id},
            headers=_headers(),
        )
        assert passed.status_code == 200
        assert len(passed.json()) == 2

        offered = client.post(
            f"/api/case/{case_id}/offer",
            json={},
            headers=_headers(),
        )
        assert offered.status_code == 200

        onboarded = client.post(
            f"/api/case/{case_id}/onboard",
            json={},
            headers=_headers(),
        )
        assert onboarded.status_code == 200

        detail = client.get(f"/api/case/{case_id}", headers=_headers())
        assert detail.status_code == 200
        assert detail.json()["stage"] == "入职"
        assert len(detail.json()["rounds"]) == 2
        assert len(detail.json()["events"]) >= 6

        overview = client.get("/api/dashboard/overview", headers=_headers())
        assert overview.status_code == 200
        assert overview.json()["recommendation_total"] == 1
        assert overview.json()["offer_total"] == 1

        by_jd = client.get("/api/dashboard/by-jd", headers=_headers())
        assert by_jd.status_code == 200
        assert by_jd.json()[0]["offer_total"] == 1


def test_dashboard_trend_and_export_endpoints(app, tmp_path: Path) -> None:
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session:
            candidate = Candidate(display_name="张三")
            session.add(candidate)
            session.commit()
            candidate_id = candidate.id

        jd_id = _seed_jd(client)
        created = client.post(
            "/api/case",
            json={"candidate_id": candidate_id, "jd_id": jd_id},
            headers=_headers(),
        )
        case_id = created.json()["id"]
        client.post(f"/api/case/{case_id}/recommend", json={}, headers=_headers())
        client.post(f"/api/case/{case_id}/offer", json={}, headers=_headers())

        trend = client.get("/api/dashboard/trend?granularity=month", headers=_headers())
        assert trend.status_code == 200
        assert trend.json()[0]["recommendation"] == 1

        exported = client.get("/api/dashboard/export", headers=_headers())
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
