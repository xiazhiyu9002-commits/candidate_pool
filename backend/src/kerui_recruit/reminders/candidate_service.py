"""候选人提醒：人才库行内建立的个人待办。

与 ``reminders.service`` 的「流程提醒」严格分开：

- 没有日期概念：建好即出现在「今日待办」，直到被勾选完成。
- 不进邮件，也不受招聘流程状态影响（流程提醒会被终态自动暂停并发到期邮件）。
- 人名存快照，候选人改名或删除后提醒行仍显示建立时的人名。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, CandidateReminder

MAX_CONTENT_LENGTH = 500


class CandidateReminderNotFound(LookupError):
    """候选人不存在或提醒不存在。"""


class CandidateReminderService:
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create(self, *, candidate_id: str, content: str) -> dict:
        text = (content or "").strip()
        if not text:
            raise ValueError("提醒内容不能为空")
        if len(text) > MAX_CONTENT_LENGTH:
            raise ValueError(f"提醒内容不能超过 {MAX_CONTENT_LENGTH} 字")
        with self.session_factory() as session, session.begin():
            candidate = session.get(Candidate, candidate_id)
            if candidate is None or candidate.deleted_at is not None:
                raise CandidateReminderNotFound(f"候选人不存在：{candidate_id}")
            reminder = CandidateReminder(
                candidate_id=candidate_id,
                candidate_name_snapshot=candidate.display_name,
                content=text,
            )
            session.add(reminder)
            session.flush()
            return _payload(reminder)

    def list_open(self) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(CandidateReminder)
                .where(CandidateReminder.done.is_(False))
                .order_by(CandidateReminder.created_at.asc())
            ).all()
            return [_payload(row) for row in rows]

    def mark_done(self, reminder_id: str) -> dict:
        """勾选 = 任务完成：置为完成并从「今日待办」移出（幂等）。"""
        with self.session_factory() as session, session.begin():
            reminder = session.get(CandidateReminder, reminder_id)
            if reminder is None:
                raise CandidateReminderNotFound(f"提醒不存在：{reminder_id}")
            if not reminder.done:
                reminder.done = True
                reminder.done_at = datetime.now(timezone.utc)
            return _payload(reminder)


def _payload(reminder: CandidateReminder) -> dict:
    return {
        "id": reminder.id,
        "candidate_id": reminder.candidate_id,
        "name": reminder.candidate_name_snapshot,
        "content": reminder.content,
        "done": reminder.done,
    }
