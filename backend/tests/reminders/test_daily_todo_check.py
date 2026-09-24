"""今日待办系统项的当日勾选：勾选只表示「今天处理过」，不代表任务完成，次日重置。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.core.settings import Settings
from kerui_recruit.daily_followup.service import SHANGHAI, DailyFollowupService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CandidateJobCase, CaseEvent, DailyTodoCheck, Jd
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.runtime import create_runtime_app


@pytest.fixture()
def factory(tmp_path) -> sessionmaker:
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def seed_recommended(factory) -> tuple[str, str]:
    """造一条「已推荐但未反馈」的流程：它会出现在今日待办的「追反馈」列。"""
    with factory() as session, session.begin():
        candidate = Candidate(display_name="张三", status="AVAILABLE")
        jd = Jd(company="某公司", title="工程师", status="OPEN")
        session.add_all([candidate, jd])
        session.flush()
        case = CandidateJobCase(candidate_id=candidate.id, jd_id=jd.id, stage="已推荐")
        session.add(case)
        session.flush()
        session.add(CaseEvent(
            case_id=case.id,
            event_type="RECOMMENDED",
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        session.flush()
        return candidate.id, case.id


def test_checked_items_are_scoped_to_the_day(factory) -> None:
    """勾选按天存储：次日查不到当天记录，即自动回到未勾选，无需定时重置。"""
    service = DailyFollowupService(session_factory=factory)
    service.set_checked(day="2026-09-20", item_key="followup:case-1", done=True)

    assert service.checked_item_keys("2026-09-20") == {"followup:case-1"}
    assert service.checked_item_keys("2026-09-21") == set()


def test_set_checked_is_idempotent_and_reversible(factory) -> None:
    service = DailyFollowupService(session_factory=factory)

    service.set_checked(day="2026-09-20", item_key="interview:case-2", done=True)
    service.set_checked(day="2026-09-20", item_key="interview:case-2", done=True)
    with factory() as session:
        assert len(session.scalars(select(DailyTodoCheck)).all()) == 1

    service.set_checked(day="2026-09-20", item_key="interview:case-2", done=False)
    with factory() as session:
        assert session.scalars(select(DailyTodoCheck)).all() == []


def _client(tmp_path) -> TestClient:
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test"))
    )
    return app, TestClient(app)


def _headers() -> dict:
    return {"X-Kerui-Session": "test"}


def test_today_items_expose_stable_keys_and_reflect_checks(tmp_path) -> None:
    """待办项必须带稳定键，否则前端无法记录「今天处理过哪一项」。"""
    app, client = _client(tmp_path)
    _, case_id = seed_recommended(app.state.services.session_factory)

    body = client.get("/api/daily-followup/today", headers=_headers()).json()
    assert len(body["followup"]) == 1
    item = body["followup"][0]
    assert item["item_key"] == f"followup:{case_id}"
    assert item["done"] is False
    assert item["name"] == "张三"

    response = client.post(
        "/api/daily-followup/check",
        headers=_headers(),
        json={"item_key": item["item_key"], "done": True},
    )
    assert response.status_code == 200

    body = client.get("/api/daily-followup/today", headers=_headers()).json()
    assert body["followup"][0]["done"] is True
    # 勾选只记「今天」：记录里带的是上海日期。
    with app.state.services.session_factory() as session:
        row = session.scalar(select(DailyTodoCheck))
        assert row is not None
        assert row.check_date == datetime.now(SHANGHAI).date().isoformat()


def test_check_endpoint_rejects_unknown_fields(tmp_path) -> None:
    _app, client = _client(tmp_path)
    response = client.post(
        "/api/daily-followup/check",
        headers=_headers(),
        json={"item_key": "followup:x", "done": True, "unexpected": 1},
    )
    assert response.status_code == 422


def test_check_endpoint_requires_session(tmp_path) -> None:
    _app, client = _client(tmp_path)
    response = client.post("/api/daily-followup/check", json={"item_key": "followup:x"})
    assert response.status_code == 401
