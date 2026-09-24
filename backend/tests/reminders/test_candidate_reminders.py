"""候选人提醒：人才库行内建立，出现在「今日待办」，勾选即完成并移出。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.reminders.candidate_service import CandidateReminderService
from kerui_recruit.runtime import create_runtime_app


@pytest.fixture()
def factory(tmp_path) -> sessionmaker:
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _candidate(factory, name: str = "张三") -> str:
    with factory() as session, session.begin():
        candidate = Candidate(display_name=name, status="AVAILABLE")
        session.add(candidate)
        session.flush()
        return candidate.id


def test_create_and_complete_keeps_only_open_reminders(factory) -> None:
    service = CandidateReminderService(session_factory=factory)
    candidate_id = _candidate(factory)

    created = service.create(candidate_id=candidate_id, content="下周一电话回访")
    assert created["name"] == "张三"
    assert created["content"] == "下周一电话回访"
    assert created["done"] is False
    assert [row["id"] for row in service.list_open()] == [created["id"]]

    service.mark_done(created["id"])
    assert service.list_open() == []


def test_mark_done_is_idempotent(factory) -> None:
    service = CandidateReminderService(session_factory=factory)
    created = service.create(candidate_id=_candidate(factory), content="回访")

    first = service.mark_done(created["id"])
    second = service.mark_done(created["id"])

    assert first["done"] is True and second["done"] is True


def test_name_snapshot_survives_a_rename(factory) -> None:
    """「固定显示人名」：候选人改名后，提醒行仍显示建立时的人名。"""
    service = CandidateReminderService(session_factory=factory)
    candidate_id = _candidate(factory, name="张三")
    service.create(candidate_id=candidate_id, content="回访")

    with factory() as session, session.begin():
        session.get(Candidate, candidate_id).display_name = "张三丰"

    assert service.list_open()[0]["name"] == "张三"


def test_create_rejects_blank_content(factory) -> None:
    service = CandidateReminderService(session_factory=factory)
    with pytest.raises(ValueError):
        service.create(candidate_id=_candidate(factory), content="   ")


def _client(tmp_path):
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test"))
    )
    return app, TestClient(app)


def _headers() -> dict:
    return {"X-Kerui-Session": "test"}


def test_reminder_shows_up_in_today_todo_and_disappears_when_done(tmp_path) -> None:
    app, client = _client(tmp_path)
    candidate_id = _candidate(app.state.services.session_factory, name="李四")

    created = client.post(
        "/api/candidate-reminders",
        headers=_headers(),
        json={"candidate_id": candidate_id, "content": "问一下期望薪资"},
    )
    assert created.status_code == 200

    today = client.get("/api/daily-followup/today", headers=_headers()).json()
    assert today["reminders"] == [
        {
            "id": created.json()["id"],
            "candidate_id": candidate_id,
            "name": "李四",
            "content": "问一下期望薪资",
            "done": False,
        }
    ]

    assert client.post(
        f"/api/candidate-reminders/{created.json()['id']}/done", headers=_headers()
    ).status_code == 200

    today = client.get("/api/daily-followup/today", headers=_headers()).json()
    assert today["reminders"] == []


def test_reminder_api_rejects_unknown_candidate(tmp_path) -> None:
    _app, client = _client(tmp_path)
    response = client.post(
        "/api/candidate-reminders",
        headers=_headers(),
        json={"candidate_id": "missing", "content": "回访"},
    )
    assert response.status_code == 404


def test_reminder_api_rejects_blank_content(tmp_path) -> None:
    app, client = _client(tmp_path)
    candidate_id = _candidate(app.state.services.session_factory)
    response = client.post(
        "/api/candidate-reminders",
        headers=_headers(),
        json={"candidate_id": candidate_id, "content": ""},
    )
    assert response.status_code == 422
