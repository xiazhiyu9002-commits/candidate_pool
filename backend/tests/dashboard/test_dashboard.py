from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.cases.service import CaseService, SHANGHAI
from kerui_recruit.dashboard.service import DashboardFilters, DashboardService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.db.session import create_engine_for


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(session_factory: sessionmaker[Session], *, n_candidates: int = 3, company: str = "某公司", title: str = "后端"):
    with session_factory() as session:
        candidates = [Candidate(display_name=f"候选人{i}") for i in range(n_candidates)]
        jd = Jd(company=company, title=title, status="OPEN")
        session.add_all([*candidates, jd])
        session.commit()
        return [c.id for c in candidates], jd.id


def test_full_pipeline_syncs_dashboard(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=2)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)

    svc.recommend(case.id)
    entered = svc.enter_interview(case.id)
    svc.pass_and_advance(case.id, case_round_id=entered.case_round_id)
    svc.offer(case.id)
    svc.onboard(case.id)

    dash = DashboardService(session_factory)
    overview = dash.overview()
    assert overview["recommendation_total"] == 1
    assert overview["offer_total"] == 1

    by_jd = dash.by_jd()
    assert len(by_jd) == 1
    jd_metrics = by_jd[0]
    assert jd_metrics["recommendation_total"] == 1
    assert jd_metrics["offer_total"] == 1
    assert jd_metrics["final_offer_rate"] == 1.0

    rounds = {r["round_no"]: r for r in jd_metrics["rounds"]}
    assert rounds[1]["passed"] == 1 and rounds[1]["failed"] == 0
    assert rounds[1]["pass_rate"] == 1.0
    # 第 2 轮已进入但无结果，分母为 0 显示 None。
    assert rounds[2]["judged"] == 0 and rounds[2]["pass_rate"] is None


def test_pass_rate_excludes_pending(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=3)
    svc = CaseService(session_factory)
    cases = [svc.create(candidate_id=cid, jd_id=jd_id) for cid in candidate_ids]

    for case in cases:
        svc.recommend(case.id)
        entered = svc.enter_interview(case.id)
    # 通过、未通过、待反馈各一。
    svc.record_result(cases[0].id, case_round_id=svc.get_rounds(cases[0].id)[0].id, result="通过")
    svc.record_result(cases[1].id, case_round_id=svc.get_rounds(cases[1].id)[0].id, result="未通过")
    svc.record_result(cases[2].id, case_round_id=svc.get_rounds(cases[2].id)[0].id, result="待反馈")

    by_jd = DashboardService(session_factory).by_jd()
    rounds = {r["round_no"]: r for r in by_jd[0]["rounds"]}
    r1 = rounds[1]
    assert r1["passed"] == 1 and r1["failed"] == 1 and r1["pending"] == 1
    assert r1["pass_rate"] == pytest.approx(0.5)


def test_idempotent_recommend_and_offer(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)

    svc.recommend(case.id, idempotency_key="rec-1")
    svc.recommend(case.id, idempotency_key="rec-1")  # 重复点击不重复计
    svc.offer(case.id, idempotency_key="offer-1")
    svc.offer(case.id, idempotency_key="offer-1")

    overview = DashboardService(session_factory).overview()
    assert overview["recommendation_total"] == 1
    assert overview["offer_total"] == 1


def test_offer_without_recommend_no_invalid_rate(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)
    svc.offer(case.id)  # 直接补录 Offer，无推荐记录

    by_jd = DashboardService(session_factory).by_jd()
    assert by_jd[0]["offer_total"] == 1
    assert by_jd[0]["recommendation_total"] == 0
    assert by_jd[0]["final_offer_rate"] is None  # 不产生 >100%


def test_cross_month_recommend_and_offer(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)

    jan = datetime(2026, 1, 15, 10, 0, tzinfo=SHANGHAI)
    feb = datetime(2026, 2, 10, 10, 0, tzinfo=SHANGHAI)
    svc.recommend(case.id, occurred_at=jan)
    svc.offer(case.id, occurred_at=feb)

    trend = DashboardService(session_factory).trend("month")
    by_period = {b["period"]: b for b in trend}
    assert by_period["2026-01"]["recommendation"] == 1
    assert by_period["2026-01"]["offer"] == 0
    assert by_period["2026-02"]["offer"] == 1


def test_shanghai_timezone_boundary(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)

    # UTC 2025-12-31 16:30 == 上海 2026-01-01 00:30，应归 2026-01。
    occurred = datetime(2025, 12, 31, 16, 30, tzinfo=timezone.utc)
    svc.recommend(case.id, occurred_at=occurred)

    trend = DashboardService(session_factory).trend("month")
    assert trend[0]["period"] == "2026-01"
    assert trend[0]["recommendation"] == 1


def test_void_event_restores_metrics(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)
    event = svc.recommend(case.id)

    assert DashboardService(session_factory).overview()["recommendation_total"] == 1
    svc.void_event(event.id, note="误操作")
    assert DashboardService(session_factory).overview()["recommendation_total"] == 0


def test_template_change_does_not_rewrite_history(session_factory: sessionmaker[Session]) -> None:
    candidate_ids, jd_id = _seed(session_factory, n_candidates=1)
    svc = CaseService(session_factory)
    svc.set_process(jd_id, [{"round_no": 1, "round_name": "技术面"}, {"round_no": 2, "round_name": "HR面"}])

    case = svc.create(candidate_id=candidate_ids[0], jd_id=jd_id)
    entered = svc.enter_interview(case.id)
    assert entered.case_round_id is not None
    rounds = svc.get_rounds(case.id)
    assert rounds[0].round_name == "技术面"

    # 改名后不影响已有轮次实例。
    svc.set_process(jd_id, [{"round_no": 1, "round_name": "电话面"}])
    assert svc.get_rounds(case.id)[0].round_name == "技术面"


def test_empty_and_zero_buckets(session_factory: sessionmaker[Session]) -> None:
    _seed(session_factory, n_candidates=1)
    dash = DashboardService(session_factory)
    assert dash.overview()["recommendation_total"] == 0
    assert dash.by_jd() == []

    from datetime import datetime
    trend = dash.trend(
        "month",
        DashboardFilters(
            date_from=datetime(2026, 1, 1, tzinfo=SHANGHAI),
            date_to=datetime(2026, 3, 31, tzinfo=SHANGHAI),
        ),
    )
    assert [b["period"] for b in trend] == ["2026-01", "2026-02", "2026-03"]
    assert all(b["recommendation"] == 0 and b["offer"] == 0 for b in trend)


def test_conversion_uses_recommended_cohort_not_all_offers(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=2)
    service = CaseService(session_factory)
    cases = [service.create(candidate_id=cid, jd_id=jd_id) for cid in ids]
    service.recommend(cases[0].id)
    for case in cases:
        service.offer(case.id)
    metrics = DashboardService(session_factory).by_jd()[0]
    assert metrics["final_offer_rate"] == 1.0
    assert metrics["offer_total"] == 2
    assert metrics["unattributed_offer_total"] == 1


def test_round_uses_latest_result_and_void_restores_previous_result(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    entered = service.enter_interview(case.id)
    service.record_result(case.id, case_round_id=entered.case_round_id, result="待反馈")
    service.record_result(case.id, case_round_id=entered.case_round_id, result="未通过")
    passed = service.record_result(case.id, case_round_id=entered.case_round_id, result="通过")
    dashboard = DashboardService(session_factory)
    result = dashboard.by_jd()[0]["rounds"][0]
    assert (result["judged"], result["passed"], result["failed"], result["pending"]) == (1, 1, 0, 0)
    service.void_event(passed.id)
    result = dashboard.by_jd()[0]["rounds"][0]
    assert (result["judged"], result["passed"], result["failed"]) == (1, 0, 1)


def test_offer_status_change_does_not_create_new_period_offer(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    service.offer(case.id, occurred_at=datetime(2026, 1, 10, tzinfo=SHANGHAI))
    service.update_offer(case.id, result="已接受", occurred_at=datetime(2026, 2, 10, tzinfo=SHANGHAI))
    filters = DashboardFilters(date_from=datetime(2026, 2, 1), date_to=datetime(2026, 2, 28))
    dashboard = DashboardService(session_factory)
    assert dashboard.overview(filters)["offer_total"] == 0
    assert dashboard.trend("month", filters) == [{"period": "2026-02", "recommendation": 0, "offer": 0}]


def test_date_filter_cannot_join_unrelated_events_to_rounds(session_factory):
    import warnings
    ids, jd_id = _seed(session_factory, n_candidates=2)
    service = CaseService(session_factory)
    old = service.create(candidate_id=ids[0], jd_id=jd_id)
    other = service.create(candidate_id=ids[1], jd_id=jd_id)
    service.enter_interview(old.id, occurred_at=datetime(2026, 1, 10, tzinfo=SHANGHAI))
    service.recommend(other.id, occurred_at=datetime(2026, 2, 10, tzinfo=SHANGHAI))
    filters = DashboardFilters(date_from=datetime(2026, 2, 1), date_to=datetime(2026, 2, 28))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rows = DashboardService(session_factory).by_jd(filters)
    assert rows[0]["rounds"] == []


def test_void_entering_round_removes_its_result_from_metrics(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    entered = service.enter_interview(case.id)
    service.record_result(case.id, case_round_id=entered.case_round_id, result="通过")
    service.void_event(entered.id)
    assert DashboardService(session_factory).by_jd() == []


def test_zero_weeks_include_monday_after_sunday(session_factory):
    filters = DashboardFilters(date_from=datetime(2026, 2, 1), date_to=datetime(2026, 2, 2))
    assert [row["period"] for row in DashboardService(session_factory).trend("week", filters)] == ["2026-W05", "2026-W06"]


def test_repeat_recommendation_is_not_a_new_month_recommendation(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    service.recommend(case.id, occurred_at=datetime(2026, 1, 10, tzinfo=SHANGHAI))
    service.recommend(case.id, occurred_at=datetime(2026, 2, 10, tzinfo=SHANGHAI))
    filters = DashboardFilters(date_from=datetime(2026, 2, 1), date_to=datetime(2026, 2, 28))
    assert DashboardService(session_factory).overview(filters)["recommendation_total"] == 0


def test_future_dated_events_included_when_no_date_filter(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    service.recommend(case.id, occurred_at=future)
    service.offer(case.id, occurred_at=future)

    overview = DashboardService(session_factory).overview()
    assert overview["recommendation_total"] == 1
    assert overview["offer_total"] == 1


def test_overview_onboarded_and_active_offer(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=2)
    service = CaseService(session_factory)
    # A：offer 后入职；B：仅 offer，仍在有效 offer 状态。
    a = service.create(candidate_id=ids[0], jd_id=jd_id)
    service.recommend(a.id)
    service.offer(a.id)
    service.onboard(a.id)
    b = service.create(candidate_id=ids[1], jd_id=jd_id)
    service.recommend(b.id)
    service.offer(b.id)

    overview = DashboardService(session_factory).overview()
    assert overview["offer_total"] == 2
    assert overview["active_offer_total"] == 1
    assert overview["onboarded_total"] == 1


def test_unattributed_offer_remains_active_offer(session_factory):
    ids, jd_id = _seed(session_factory, n_candidates=1)
    service = CaseService(session_factory)
    case = service.create(candidate_id=ids[0], jd_id=jd_id)
    service.offer(case.id)  # 直接补录 offer，无推荐记录

    overview = DashboardService(session_factory).overview()
    assert overview["offer_total"] == 1
    assert overview["active_offer_total"] == 1
    assert overview["onboarded_total"] == 0
