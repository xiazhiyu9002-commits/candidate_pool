from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.runtime import create_runtime_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("launch-token"),
    )
    app = create_runtime_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "launch-token"}


def test_diagnostics_collect_and_export(client: TestClient) -> None:
    resp = client.get("/api/diagnostics", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "counts" in body
    assert "sqlite_version" in body

    export = client.get("/api/diagnostics/export", headers=_headers())
    assert export.status_code == 200
    assert export.headers["content-type"] == "application/json"


def test_reminder_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/reminders",
        json={"title": "联系候选人", "remind_at": "2030-01-01T00:00:00Z"},
        headers=_headers(),
    )
    assert created.status_code == 200
    rid = created.json()["id"]

    pending = client.get("/api/reminders", headers=_headers())
    assert pending.status_code == 200
    assert any(r["id"] == rid for r in pending.json())

    dismissed = client.post(f"/api/reminders/{rid}/dismiss", headers=_headers())
    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed"] is True


def test_mapping_project_and_tree(client: TestClient) -> None:
    proj = client.post(
        "/api/mapping/projects",
        json={"name": "互联网公司图谱"},
        headers=_headers(),
    )
    assert proj.status_code == 200
    pid = proj.json()["id"]

    snap = client.post(
        f"/api/mapping/projects/{pid}/build-from-text",
        json={"text": "字节跳动\n  技术部\n腾讯", "label": "v1"},
        headers=_headers(),
    )
    assert snap.status_code == 200
    sid = snap.json()["id"]

    tree = client.get(f"/api/mapping/snapshots/{sid}/tree", headers=_headers())
    assert tree.status_code == 200
    roots = tree.json()
    assert len(roots) == 2
    assert roots[0]["name"] == "字节跳动"
    assert roots[0]["children"][0]["name"] == "技术部"

    pdf = client.get(f"/api/mapping/snapshots/{sid}/export-pdf", headers=_headers())
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


def test_backup_snapshot_list(client: TestClient) -> None:
    created = client.post("/api/backup/snapshots", json={"label": "手动备份"}, headers=_headers())
    assert created.status_code == 200
    assert created.json()["filename"]

    snapshots = client.get("/api/backup/snapshots", headers=_headers())
    assert snapshots.status_code == 200
    assert len(snapshots.json()) >= 1


def test_bd_search_returns_empty_with_offline_provider(client: TestClient) -> None:
    resp = client.post(
        "/api/bd/search",
        json={"query": "Java 工程师 招聘", "limit": 5},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_onboarding_status_reports_configuration_and_health(client: TestClient) -> None:
    resp = client.get("/api/onboarding/status", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "data_root" in body
    assert body["llm_enabled"] is False
    assert body["search_enabled"] is False
    assert body["health"]["database"]["status"] == "healthy"
