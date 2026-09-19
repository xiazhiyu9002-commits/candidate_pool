from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.cases.service import CaseService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.db.session import create_engine_for


@pytest.mark.parametrize("deleted_entity", ["candidate", "jd", "case"])
def test_historical_backfill_cannot_bypass_deleted_entity(session_factory, deleted_entity):
    from datetime import datetime
    from kerui_recruit.db.models import CandidateJobCase
    from kerui_recruit.cases.service import CaseStateError
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.onboard(case.id, occurred_at=datetime(2026, 3, 1))
    entities = {"candidate": (Candidate, candidate_id), "jd": (Jd, jd_id), "case": (CandidateJobCase, case.id)}
    entity_type, entity_id = entities[deleted_entity]
    with session_factory() as session, session.begin():
        session.get(entity_type, entity_id).deleted_at = datetime(2026, 3, 2)
    with pytest.raises(CaseStateError, match="删除"):
        service.recommend(case.id, occurred_at=datetime(2026, 1, 1))
    with pytest.raises(CaseStateError, match="删除"):
        service.offer(case.id, occurred_at=datetime(2026, 2, 1))
    assert len(service.get_timeline(case.id)) == 1

def test_legacy_pass_retry_recovers_both_events_without_writing(session_factory):
    from kerui_recruit.db.models import CaseEvent
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    first = service.pass_and_advance(case.id, case_round_id=entered.case_round_id, idempotency_key="old-client")
    with session_factory() as session, session.begin():
        session.get(CaseEvent, first[0].id).idempotency_key = None
    repeated = service.pass_and_advance(case.id, case_round_id=entered.case_round_id, idempotency_key="old-client")
    assert [event.id for event in repeated] == [event.id for event in first]
    assert len(service.get_timeline(case.id)) == 3


def test_void_deletes_event_and_repeated_void_raises(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    event = service.recommend(case.id, note="首次推荐")
    service.void_event(event.id, note="误操作")
    assert len(service.get_timeline(case.id)) == 0
    with pytest.raises(LookupError, match="not found"):
        service.void_event(event.id, note="误操作")


def test_backfilled_recommendation_and_offer_remain_recordable_after_onboarding(session_factory):
    from datetime import datetime
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    onboarded = service.onboard(case.id, occurred_at=datetime(2026, 3, 1))
    service.recommend(case.id, occurred_at=datetime(2026, 1, 1), note="补录历史推荐")
    service.offer(case.id, occurred_at=datetime(2026, 2, 1), note="补录历史Offer")
    assert service.get(case.id).stage == "入职"
    with session_factory() as session:
        assert session.get(Candidate, candidate_id).status == "ON_HOLD"
    with pytest.raises(ValueError, match="候选人"):
        service.recommend(case.id, occurred_at=datetime(2026, 3, 2))
    service.void_event(onboarded.id)
    assert service.get(case.id).stage == "Offer"


@pytest.mark.parametrize("candidate_status", ["ON_HOLD", "PENDING_REVIEW", "ARCHIVED"])
def test_new_case_requires_available_candidate_but_existing_case_stays_readable(session_factory, candidate_status):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    _, other_jd = _seed(session_factory)
    with session_factory() as session, session.begin():
        session.get(Candidate, candidate_id).status = candidate_status
    with pytest.raises(ValueError, match="候选人"):
        service.create(candidate_id=candidate_id, jd_id=other_jd)
    assert service.create(candidate_id=candidate_id, jd_id=jd_id).id == case.id
    assert service.get(case.id).candidate_id == candidate_id


@pytest.mark.parametrize("job_status", ["DRAFT", "PAUSED", "FILLED", "CANCELLED", "ARCHIVED"])
def test_new_case_requires_open_job(session_factory, job_status):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    with session_factory() as session, session.begin():
        session.get(Jd, jd_id).status = job_status
    with pytest.raises(ValueError, match="岗位"):
        service.create(candidate_id=candidate_id, jd_id=jd_id)


@pytest.mark.parametrize("candidate_status", ["ON_HOLD", "PENDING_REVIEW"])
@pytest.mark.parametrize("action", ["recommend", "enter", "pass", "offer", "onboard", "offer_onboard"])
def test_unavailable_candidate_cannot_start_new_progress(session_factory, candidate_status, action):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    with session_factory() as session, session.begin():
        session.get(Candidate, candidate_id).status = candidate_status
    actions = {
        "recommend": lambda: service.recommend(case.id),
        "enter": lambda: service.enter_interview(case.id),
        "pass": lambda: service.pass_and_advance(case.id, case_round_id=entered.case_round_id),
        "offer": lambda: service.offer(case.id),
        "onboard": lambda: service.onboard(case.id),
        "offer_onboard": lambda: service.update_offer(case.id, result="已入职"),
    }
    with pytest.raises(ValueError, match="候选人"):
        actions[action]()
    assert len(service.get_timeline(case.id)) == 1
    assert len(service.get_rounds(case.id)) == 1


def test_unavailable_candidate_can_correct_historical_result_and_void(session_factory):
    from datetime import datetime
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id, occurred_at=datetime(2026, 1, 1))
    with session_factory() as session, session.begin():
        session.get(Candidate, candidate_id).status = "ON_HOLD"
        session.get(Jd, jd_id).status = "FILLED"
    failed = service.record_result(case.id, case_round_id=entered.case_round_id, result="未通过", occurred_at=datetime(2026, 1, 2))
    passed = service.record_result(case.id, case_round_id=entered.case_round_id, result="通过", occurred_at=datetime(2026, 1, 3))
    assert service.get(case.id).stage == "初试"
    service.void_event(passed.id)
    assert service.get(case.id).stage == "客户拒绝"
    service.void_event(failed.id)
    assert service.get(case.id).stage == "初试"
    with session_factory() as session:
        assert session.get(Candidate, candidate_id).status == "ON_HOLD"


def test_onboarding_pauses_all_candidate_reminders_and_void_restores_only_eligible_ones(session_factory):
    from datetime import datetime, timedelta, timezone
    from kerui_recruit.reminders.service import ReminderService
    candidate_id, jd_id = _seed(session_factory)
    _, other_jd = _seed(session_factory)
    _, closed_jd = _seed(session_factory)
    _, exited_jd = _seed(session_factory)
    service = CaseService(session_factory)
    first = service.create(candidate_id=candidate_id, jd_id=jd_id)
    other = service.create(candidate_id=candidate_id, jd_id=other_jd)
    closed = service.create(candidate_id=candidate_id, jd_id=closed_jd)
    exited = service.create(candidate_id=candidate_id, jd_id=exited_jd)
    reminders = ReminderService(session_factory)
    due = datetime.now(timezone.utc) - timedelta(days=1)
    first_reminder = reminders.create(title="入职岗位", remind_at=due, case_id=first.id)
    other_reminder = reminders.create(title="其他开放岗位", remind_at=due, case_id=other.id)
    reminders.create(title="关闭岗位", remind_at=due, case_id=closed.id)
    reminders.create(title="退出岗位", remind_at=due, case_id=exited.id)
    completed = reminders.create(title="已完成", remind_at=due, case_id=other.id)
    reminders.dismiss(completed.id)
    independent = reminders.create(title="独立提醒", remind_at=due)
    service.exit(exited.id, result="候选人退出")
    with session_factory() as session, session.begin():
        session.get(Jd, closed_jd).status = "CANCELLED"
    onboarded = service.onboard(first.id)
    assert {r.id for r in reminders.list_due()} == {independent.id}
    service.void_event(onboarded.id)
    assert {r.id for r in reminders.list_due()} == {first_reminder.id, other_reminder.id, independent.id}


def test_duplicate_enter_waits_for_effective_current_round_result(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    with pytest.raises(ValueError, match="反馈|结果"):
        service.enter_interview(case.id)
    service.record_result(case.id, case_round_id=entered.case_round_id, result="待反馈")
    with pytest.raises(ValueError, match="反馈|结果"):
        service.enter_interview(case.id)
    service.record_result(case.id, case_round_id=entered.case_round_id, result="通过")
    service.enter_interview(case.id)
    assert [r.round_no for r in service.get_rounds(case.id)] == [1, 2]


def test_interview_stage_follows_current_round(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.recommend(case.id)
    entered = service.enter_interview(case.id)
    assert service.get(case.id).stage == "初试"
    service.record_result(case.id, case_round_id=entered.case_round_id, result="通过")
    entered2 = service.enter_interview(case.id)
    assert service.get(case.id).stage == "复试"
    service.record_result(case.id, case_round_id=entered2.case_round_id, result="通过")
    service.enter_interview(case.id)
    assert service.get(case.id).stage == "终试"


def test_final_template_round_requires_explicit_result_or_named_add_on(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    service.set_process(jd_id, [{"round_no": 1, "round_name": "终面"}])
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    with pytest.raises(ValueError, match="最后|最终|终轮"):
        service.pass_and_advance(case.id, case_round_id=entered.case_round_id)
    assert len(service.get_timeline(case.id)) == 1
    service.record_result(case.id, case_round_id=entered.case_round_id, result="通过")
    with pytest.raises(ValueError, match="最后|最终|终轮"):
        service.enter_interview(case.id)
    assert len(service.get_rounds(case.id)) == 1
    added = service.enter_interview(case.id, round_name="明确加面")
    assert service.get_rounds(case.id)[1].round_name == "明确加面"
    assert added.case_round_id != entered.case_round_id


def test_pass_retry_returns_the_same_full_pair_and_old_round_cannot_advance_twice(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    first = service.pass_and_advance(case.id, case_round_id=entered.case_round_id, idempotency_key="click-1")
    repeated = service.pass_and_advance(case.id, case_round_id=entered.case_round_id, idempotency_key="click-1")
    assert [event.id for event in repeated] == [event.id for event in first]
    assert [event.event_type for event in repeated] == ["INTERVIEW_RESULT", "INTERVIEW_ENTERED"]
    with pytest.raises(ValueError, match="当前|轮次"):
        service.pass_and_advance(case.id, case_round_id=entered.case_round_id, idempotency_key="different-click")
    assert len(service.get_rounds(case.id)) == 2
    assert len(service.get_timeline(case.id)) == 3


def test_voided_entry_cannot_be_used_to_advance(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    service.void_event(entered.id)
    with pytest.raises(LookupError, match="not found"):
        service.pass_and_advance(case.id, case_round_id=entered.case_round_id)
    assert len(service.get_rounds(case.id)) == 0



@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(session_factory: sessionmaker[Session]) -> tuple[str, str]:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        jd = Jd(company="某公司", title="Java", status="OPEN")
        session.add_all([candidate, jd])
        session.commit()
        return candidate.id, jd.id


def test_create_case_is_idempotent(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)

    first = service.create(candidate_id=candidate_id, jd_id=jd_id)
    second = service.create(candidate_id=candidate_id, jd_id=jd_id)

    assert first.id == second.id
    assert first.stage == "待评估"


def test_recommend_records_event(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)

    event = service.recommend(case.id, note="推荐给客户")
    assert event.event_type == "RECOMMENDED"
    assert service.get(case.id).stage == "已推荐"
    assert len(service.get_timeline(case.id)) == 1


def test_enter_interview_creates_stable_round(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)

    entered = service.enter_interview(case.id)
    assert entered.event_type == "INTERVIEW_ENTERED"
    rounds = service.get_rounds(case.id)
    assert len(rounds) == 1
    assert rounds[0].round_no == 1
    assert rounds[0].round_name == "第1轮"


def test_void_event_deletes_history(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)

    event = service.recommend(case.id)
    service.void_event(event.id, note="误操作")

    timeline = service.get_timeline(case.id)
    assert len(timeline) == 0  # 直接删除，不留作废记录
    assert service.get(case.id).stage == "待评估"


def test_record_result_rejects_unknown(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)

    with pytest.raises(ValueError, match="Unknown interview result"):
        service.record_result(case.id, case_round_id=entered.case_round_id, result="随便")


def test_list_cases_filters_by_candidate(session_factory: sessionmaker[Session]) -> None:
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory=session_factory)
    service.create(candidate_id=candidate_id, jd_id=jd_id)

    cases = service.list_cases(candidate_id=candidate_id)
    assert len(cases) == 1
    assert cases[0].candidate_id == candidate_id


def test_existing_case_keeps_remaining_template_after_edit(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    service.set_process(jd_id, [{"round_no": 1, "round_name": "技术面"}, {"round_no": 2, "round_name": "HR面"}])
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    service.set_process(jd_id, [{"round_no": 1, "round_name": "业务面"}, {"round_no": 2, "round_name": "总经理面"}])
    service.pass_and_advance(case.id, case_round_id=entered.case_round_id)
    assert [r.round_name for r in service.get_rounds(case.id)] == ["技术面", "HR面"]


def test_legacy_case_without_snapshot_is_frozen_before_template_edit(session_factory):
    from kerui_recruit.db.models import CandidateJobCase

    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    service.set_process(jd_id, [{"round_no": 1, "round_name": "技术面"}, {"round_no": 2, "round_name": "HR面"}])
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    entered = service.enter_interview(case.id)
    # Literal v6 data has no snapshot columns; NULL models that upgraded row.
    with session_factory() as session, session.begin():
        session.connection().exec_driver_sql(
            "UPDATE candidate_job_case SET template_snapshot=NULL, template_version=NULL WHERE id=?",
            (case.id,),
        )
    service.set_process(jd_id, [{"round_no": 1, "round_name": "业务面"}, {"round_no": 2, "round_name": "总经理面"}])
    service.pass_and_advance(case.id, case_round_id=entered.case_round_id)
    assert [r.round_name for r in service.get_rounds(case.id)] == ["技术面", "HR面"]


def test_active_legacy_onboarding_fact_blocks_new_case_even_if_status_is_stale(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    _, other_jd = _seed(session_factory)
    service = CaseService(session_factory)
    first = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.onboard(first.id)
    with session_factory() as session, session.begin():
        stale = session.get(Candidate, candidate_id)
        stale.status = "AVAILABLE"
        stale.workflow_previous_status = None
    with pytest.raises(ValueError, match="已入职|候选人"):
        service.create(candidate_id=candidate_id, jd_id=other_jd)


def test_idempotency_key_does_not_leak_between_cases(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    other_id, _ = _seed(session_factory)
    service = CaseService(session_factory)
    first = service.create(candidate_id=candidate_id, jd_id=jd_id)
    second = service.create(candidate_id=other_id, jd_id=jd_id)
    a = service.recommend(first.id, idempotency_key="same")
    b = service.recommend(second.id, idempotency_key="same")
    assert b.case_id == second.id
    assert a.id != b.id
    assert service.recommend(second.id, idempotency_key="same").id == b.id


def test_backfilled_recommendation_does_not_rewind_offer_stage(session_factory):
    from datetime import datetime
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.offer(case.id, occurred_at=datetime(2026, 2, 10))
    service.recommend(case.id, occurred_at=datetime(2026, 1, 10))
    assert service.get(case.id).stage == "Offer"


def test_closed_job_rejects_new_progress_but_keeps_history(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.recommend(case.id)
    with session_factory() as session, session.begin():
        session.get(Jd, jd_id).status = "CANCELLED"
    with pytest.raises(ValueError, match="岗位"):
        service.enter_interview(case.id)
    assert len(service.get_timeline(case.id)) == 1


def test_onboarding_blocks_candidate_and_void_restores_previous_status(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    event = service.onboard(case.id)
    with session_factory() as session:
        assert session.get(Candidate, candidate_id).status == "ON_HOLD"
    service.void_event(event.id)
    with session_factory() as session:
        assert session.get(Candidate, candidate_id).status == "AVAILABLE"


def test_void_entry_makes_its_result_ineffective_for_stage(session_factory):
    candidate_id, jd_id = _seed(session_factory)
    service = CaseService(session_factory)
    case = service.create(candidate_id=candidate_id, jd_id=jd_id)
    service.recommend(case.id)
    entry = service.enter_interview(case.id)
    service.record_result(case.id, case_round_id=entry.case_round_id, result="未通过")
    service.void_event(entry.id)
    assert service.get(case.id).stage == "已推荐"


def test_job_delete_and_restore_pause_only_unfinished_linked_reminders(session_factory):
    from datetime import datetime, timedelta, timezone
    from kerui_recruit.reminders.service import ReminderService
    from kerui_recruit.soft_delete.service import SoftDeleteService
    candidate_id, jd_id = _seed(session_factory)
    cases = CaseService(session_factory)
    case = cases.create(candidate_id=candidate_id, jd_id=jd_id)
    reminders = ReminderService(session_factory)
    due = datetime.now(timezone.utc) - timedelta(days=1)
    linked = reminders.create(title="面试反馈", remind_at=due, case_id=case.id)
    completed = reminders.create(title="已处理", remind_at=due, case_id=case.id)
    reminders.dismiss(completed.id)
    unrelated = reminders.create(title="独立提醒", remind_at=due)
    trash = SoftDeleteService(session_factory)
    trash.soft_delete("jd", jd_id)
    assert [r.id for r in reminders.list_due()] == [unrelated.id]
    trash.restore("jd", jd_id)
    assert {r.id for r in reminders.list_due()} == {linked.id, unrelated.id}


def test_case_exit_does_not_pause_other_candidate_cases(session_factory):
    from datetime import datetime, timedelta, timezone
    from kerui_recruit.reminders.service import ReminderService
    candidate_id, jd_id = _seed(session_factory)
    _, other_jd = _seed(session_factory)
    service = CaseService(session_factory)
    first = service.create(candidate_id=candidate_id, jd_id=jd_id)
    other = service.create(candidate_id=candidate_id, jd_id=other_jd)
    reminders = ReminderService(session_factory)
    due = datetime.now(timezone.utc) - timedelta(days=1)
    reminders.create(title="退出岗位", remind_at=due, case_id=first.id)
    keep = reminders.create(title="其他岗位", remind_at=due, case_id=other.id)
    service.exit(first.id, result="候选人退出")
    assert [r.id for r in reminders.list_due()] == [keep.id]
    with session_factory() as session:
        assert session.get(Candidate, candidate_id).status == "AVAILABLE"
