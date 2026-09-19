from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.reminders.service import ReminderService


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_create_and_list_reminders(session_factory: sessionmaker[Session]) -> None:
    service = ReminderService(session_factory=session_factory)
    now = datetime.now(timezone.utc)
    service.create(title="联系候选人张三", remind_at=now + timedelta(hours=1), note="电话沟通")
    service.create(title="发送offer给李四", remind_at=now + timedelta(days=2))

    pending = service.list_pending()
    assert len(pending) == 2
    assert pending[0].title == "联系候选人张三"
    assert pending[1].title == "发送offer给李四"


def test_list_due_returns_only_past_reminders(session_factory: sessionmaker[Session]) -> None:
    service = ReminderService(session_factory=session_factory)
    now = datetime.now(timezone.utc)
    due = service.create(title="过期提醒", remind_at=now - timedelta(hours=1))
    service.create(title="未来提醒", remind_at=now + timedelta(days=1))

    due_list = service.list_due()
    assert len(due_list) == 1
    assert due_list[0].title == "过期提醒"


def test_dismiss_reminder(session_factory: sessionmaker[Session]) -> None:
    service = ReminderService(session_factory=session_factory)
    r = service.create(title="测试提醒", remind_at=datetime.now(timezone.utc))

    dismissed = service.dismiss(r.id)
    assert dismissed.dismissed is True
    assert dismissed.dismissed_at is not None

    pending = service.list_pending()
    assert len(pending) == 0


@pytest.mark.parametrize('value', [datetime(2026, 9, 1, 9), datetime(2026, 9, 1, 9, tzinfo=timezone(timedelta(hours=8)))])
def test_shanghai_reminder_is_stored_in_utc_and_returned_with_timezone(session_factory, value):
    from kerui_recruit.api.reminders import _reminder_to_response
    service = ReminderService(session_factory=session_factory)
    service.create(title='timezone check', remind_at=value)
    persisted = service.list_pending()[0]
    assert persisted.remind_at == datetime(2026, 9, 1, 1)
    assert _reminder_to_response(persisted).remind_at == '2026-09-01T01:00:00+00:00'


def test_legacy_unzoned_reminder_keeps_shanghai_wall_time(session_factory):
    from kerui_recruit.db.models import Reminder
    from kerui_recruit.api.reminders import _reminder_to_response
    old_local = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)
    with session_factory() as session, session.begin():
        old = Reminder(title='old UI local time', remind_at=old_local, time_basis='LEGACY_SHANGHAI')
        session.add(old)
    service = ReminderService(session_factory=session_factory)
    reminders = service.list_due()
    assert len(reminders) == 1
    assert _reminder_to_response(reminders[0]).remind_at == old_local.isoformat() + '+08:00'
    service.create(title='new UTC future', remind_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert [r.title for r in service.list_pending()] == ['old UI local time', 'new UTC future']
