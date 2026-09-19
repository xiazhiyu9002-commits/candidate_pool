from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.daily_followup import service as module
from kerui_recruit.daily_followup.service import DailyFollowupService
from kerui_recruit.db.models import Candidate, Jd, CandidateJobCase, CaseRound, CaseEvent
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.migrate import migrate

@pytest.fixture
def factory(tmp_path):
    engine = create_engine_for(tmp_path / 'db.sqlite3')
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)

def seed(factory, interviews):
    with factory() as session, session.begin():
        c, jd = Candidate(display_name='张三'), Jd(company='公司', title='工程师', status='OPEN')
        session.add_all([c, jd]); session.flush()
        case = CandidateJobCase(candidate_id=c.id, jd_id=jd.id, stage='复试')
        session.add(case); session.flush()
        for index, (when, results) in enumerate(interviews):
            rnd = CaseRound(case_id=case.id, round_no=index + 1, round_name=f'{index + 1}面')
            session.add(rnd); session.flush()
            session.add(CaseEvent(case_id=case.id, case_round_id=rnd.id, event_type='INTERVIEW_ENTERED', occurred_at=when - timedelta(hours=8)))
            for offset, result in enumerate(results):
                session.add(CaseEvent(case_id=case.id, case_round_id=rnd.id, event_type='INTERVIEW_RESULT', result=result, occurred_at=when - timedelta(hours=8) + timedelta(minutes=offset + 1)))

@pytest.mark.parametrize('results, expected', [([], 1), (['待反馈'], 1), (['通过', '待反馈'], 1), (['待反馈', '通过'], 0)])
def test_latest_round_latest_result(factory, results, expected):
    seed(factory, [(datetime(2026, 9, 4, 10), ['通过']), (datetime(2026, 9, 5, 10), results)])
    data = DailyFollowupService(session_factory=factory).gather(datetime(2026, 9, 5, 12))
    assert len(data['interview_no_feedback']) == expected

def test_future_today_is_upcoming_not_overdue(factory):
    seed(factory, [(datetime(2026, 9, 5, 14), [])])
    service = DailyFollowupService(session_factory=factory)
    data = service.gather(datetime(2026, 9, 5, 9))
    assert data['interview_no_feedback'] == []
    assert len(data['today_interview']) == 1
    assert '今日待面试' in service._build_email(data)

def test_evening_then_next_morning_once(factory, monkeypatch):
    seed(factory, [(datetime(2026, 9, 6, 14), [])])
    sent = []
    service = DailyFollowupService(session_factory=factory, mail_sender=SimpleNamespace(send=lambda **kw: sent.append(kw)), to='fake@example.com')
    class Clock(datetime):
        current = datetime(2026, 9, 5, 21, 29)
        @classmethod
        def now(cls, tz=None):
            return cls.current.replace(tzinfo=tz)
    monkeypatch.setattr(module, 'datetime', Clock)
    service.send_due_reports()
    # A daytime launch may send the still-open morning slot.
    sent.clear()
    Clock.current = datetime(2026, 9, 6, 21, 30)
    # Start from a fresh state to detect evening also sending a missed morning.
    from kerui_recruit.db.models import DailyFollowupState
    with factory() as session, session.begin():
        session.query(DailyFollowupState).delete()
    Clock.current = datetime(2026, 9, 5, 21, 30)
    service.send_due_reports(); service.send_due_reports()
    assert len(sent) == 1
    Clock.current = datetime(2026, 9, 6, 8, 59)
    service.send_due_reports()
    assert len(sent) == 1
    Clock.current = datetime(2026, 9, 6, 9)
    service.send_due_reports(); service.send_due_reports()
    assert len(sent) == 2
    assert '今日待面试' in sent[-1]['body']


def test_failed_send_retries_without_marking_slot(factory, monkeypatch):
    seed(factory, [(datetime(2026, 9, 6, 14), [])])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 5, 21, 30, tzinfo=tz)
    monkeypatch.setattr(module, 'datetime', Clock)
    sent = []
    def fail(**kwargs):
        raise RuntimeError('fake delivery failure')
    service = DailyFollowupService(session_factory=factory, mail_sender=SimpleNamespace(send=fail), to='fake@example.com')
    with pytest.raises(RuntimeError, match='fake delivery failure'):
        service.send_due_reports()
    service.mail_sender = SimpleNamespace(send=lambda **kw: sent.append(kw))
    service.send_due_reports()
    # Recreating the service simulates application restart with persisted slot state.
    restarted = DailyFollowupService(session_factory=factory, mail_sender=service.mail_sender, to=service.to)
    restarted.send_due_reports()
    assert len(sent) == 1
