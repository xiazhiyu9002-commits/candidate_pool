from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Reminder


class ReminderService:
    """Create, list, and dismiss reminders."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create(
        self,
        *,
        title: str,
        remind_at: datetime,
        note: str | None = None,
        case_id: str | None = None,
    ) -> Reminder:
        with self.session_factory() as session:
            if case_id:
                from kerui_recruit.db.models import CandidateJobCase
                if session.get(CandidateJobCase, case_id) is None:
                    raise LookupError("招聘流程不存在")
            local_time = remind_at.replace(tzinfo=timezone(timedelta(hours=8))) if remind_at.tzinfo is None else remind_at
            utc_time = local_time.astimezone(timezone.utc).replace(tzinfo=None)
            reminder = Reminder(title=title, remind_at=utc_time, note=note, case_id=case_id)
            session.add(reminder)
            if case_id:
                from kerui_recruit.cases.state import refresh_links
                session.flush()
                refresh_links(session, case_id=case_id)
            session.commit()
            return reminder

    def list_pending(self) -> list[Reminder]:
        with self.session_factory() as session:
            return (
                session.scalars(
                    select(Reminder)
                    .where(Reminder.dismissed == False)
                    .order_by(case((Reminder.time_basis == "LEGACY_SHANGHAI",
                                    func.datetime(Reminder.remind_at, "-8 hours")),
                                   else_=Reminder.remind_at).asc())
                )
                .all()
            )

    def list_due(self) -> list[Reminder]:
        """Reminders whose remind_at is in the past and not dismissed."""
        with self.session_factory() as session:
            return (
                session.scalars(
                    select(Reminder)
                    .where(
                        Reminder.dismissed == False,
                        Reminder.paused_by_workflow.is_(False),
                        or_(and_(Reminder.time_basis == "UTC", Reminder.remind_at <= datetime.now(timezone.utc)),
                            and_(Reminder.time_basis == "LEGACY_SHANGHAI",
                                 Reminder.remind_at <= datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8))),
                    )
                    .order_by(Reminder.remind_at.asc())
                )
                .all()
            )

    def dismiss(self, reminder_id: str) -> Reminder:
        with self.session_factory() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                raise LookupError(f"Reminder not found: {reminder_id}")
            reminder.dismissed = True
            reminder.dismissed_at = datetime.now(timezone.utc)
            session.commit()
            return reminder
