from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.reminders.mail_service import ReminderMailService
from kerui_recruit.reminders.service import ReminderService


class FakeMailSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_send_due_reminders(session_factory: sessionmaker[Session]) -> None:
    reminder_service = ReminderService(session_factory=session_factory)
    now = datetime.now(timezone.utc)
    reminder_service.create(title="联系候选人", remind_at=now - timedelta(hours=1), note="电话沟通")

    sender = FakeMailSender()
    mail_service = ReminderMailService(
        reminder_service=reminder_service,
        mail_sender=sender,  # type: ignore[arg-type]
        to="advisor@example.com",
    )

    sent = mail_service.send_due_reminders()

    assert len(sent) == 1
    assert len(sender.sent) == 1
    assert sender.sent[0]["to"] == "advisor@example.com"
    assert sender.sent[0]["subject"] == "联系候选人"

    # 到期提醒发送后应被 dismiss。
    assert reminder_service.list_due() == []


def test_send_due_reminders_skips_when_unconfigured(
    session_factory: sessionmaker[Session],
) -> None:
    reminder_service = ReminderService(session_factory=session_factory)
    mail_service = ReminderMailService(
        reminder_service=reminder_service,
        mail_sender=None,
        to=None,
    )
    assert mail_service.send_due_reminders() == []
