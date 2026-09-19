from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.cases.projection import effective_events
from kerui_recruit.db.models import (
    Candidate,
    CandidateJobCase,
    CaseEvent,
    DailyFollowupState,
    Jd,
)

SHANGHAI = timezone(timedelta(hours=8))

# 终态：不再出现在待跟进报告里。
_TERMINAL_STAGES = ("入职", "客户拒绝", "候选人拒绝", "岗位关闭")


def _utc_to_shanghai(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(SHANGHAI).replace(tzinfo=None)


class DailyFollowupService:
    """整理每日待跟进候选人（推荐未反馈 / 明日面试 / 面试未反馈）并发送邮件。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        mail_sender=None,
        to: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.mail_sender = mail_sender
        self.to = to

    def send_due_reports(self) -> None:
        """按当前上海时间发送到期报告：21:30 晚报、09:00 早报，每天各一次。"""
        if self.mail_sender is None or not self.to:
            return
        now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
        today = now_sh.date().isoformat()

        with self.session_factory() as session:
            state = session.scalar(select(DailyFollowupState).limit(1))
            if state is None:
                state = DailyFollowupState()
                session.add(state)
                session.commit()
            last_evening = state.last_evening_date
            last_morning = state.last_morning_date

        if now_sh.time() >= time(21, 30) and last_evening != today:
            if self._send_report(now_sh):
                self._mark("evening", today)
        if time(9, 0) <= now_sh.time() < time(21, 30) and last_morning != today:
            if self._send_report(now_sh):
                self._mark("morning", today)

    def _mark(self, slot: str, today: str) -> None:
        with self.session_factory() as session:
            state = session.scalar(select(DailyFollowupState).limit(1))
            if state is None:
                return
            if slot == "evening":
                state.last_evening_date = today
            else:
                state.last_morning_date = today
            session.commit()

    def _send_report(self, now_sh: datetime) -> bool:
        data = self.gather(now_sh)
        body = self._build_email(data)
        if body is None:
            return False
        self.mail_sender.send(to=self.to, subject="每日待跟进候选人", body=body)
        return True

    def gather(self, now_sh: datetime) -> dict:
        """查询三类候选人，返回结构化数据（供测试与邮件生成共用）。"""
        today = now_sh.date()
        tomorrow = today + timedelta(days=1)

        recommended_no_feedback: list[dict] = []
        tomorrow_interview: list[dict] = []
        today_interview: list[dict] = []
        interview_no_feedback: list[dict] = []

        with self.session_factory() as session:
            cases = session.scalars(
                select(CandidateJobCase)
                .join(Jd, CandidateJobCase.jd_id == Jd.id)
                .join(Candidate, CandidateJobCase.candidate_id == Candidate.id)
                .where(
                    CandidateJobCase.deleted_at.is_(None),
                    Jd.deleted_at.is_(None),
                    Jd.status == "OPEN",
                    Candidate.deleted_at.is_(None),
                    Candidate.status == "AVAILABLE",
                    ~CandidateJobCase.stage.in_(_TERMINAL_STAGES),
                )
            ).all()

            for case in cases:
                events = effective_events(list(session.scalars(
                    select(CaseEvent).where(CaseEvent.case_id == case.id)
                ).all()))
                jd = session.get(Jd, case.jd_id)
                candidate = session.get(Candidate, case.candidate_id)
                if jd is None or candidate is None:
                    continue

                recommended = [e for e in events if e.event_type == "RECOMMENDED"]
                entered = [e for e in events if e.event_type == "INTERVIEW_ENTERED"]
                results = [e for e in events if e.event_type == "INTERVIEW_RESULT"]

                if recommended and not entered:
                    rec_sh = _utc_to_shanghai(recommended[-1].occurred_at)
                    if rec_sh.date() <= today:
                        recommended_no_feedback.append({
                            "company": jd.company,
                            "title": jd.title,
                            "name": candidate.display_name,
                            "time": rec_sh.strftime("%Y-%m-%d"),
                        })
                elif entered:
                    latest = entered[-1]
                    round_results = [e for e in results if e.case_round_id == latest.case_round_id]
                    if round_results and round_results[-1].result != "待反馈":
                        continue
                    ent_sh = _utc_to_shanghai(latest.occurred_at)
                    item = {
                        "company": jd.company,
                        "title": jd.title,
                        "name": candidate.display_name,
                        "time": ent_sh.strftime("%Y-%m-%d %H:%M"),
                    }
                    if ent_sh.date() == tomorrow:
                        tomorrow_interview.append(item)
                    elif ent_sh > now_sh and ent_sh.date() == today:
                        today_interview.append(item)
                    elif ent_sh <= now_sh:
                        interview_no_feedback.append(item)

        return {
            "recommended_no_feedback": recommended_no_feedback,
            "tomorrow_interview": tomorrow_interview,
            "today_interview": today_interview,
            "interview_no_feedback": interview_no_feedback,
        }

    def _build_email(self, data: dict) -> str | None:
        if not any(data.values()):
            return None
        lines = ["每日待跟进候选人：", ""]
        if data["recommended_no_feedback"]:
            lines.append("推荐未反馈：")
            for item in data["recommended_no_feedback"]:
                lines.append(f"{item['company']}-{item['title']}-{item['name']}-{item['time']}")
            lines.append("")
        if data.get("today_interview"):
            lines.append("今日待面试：")
            for item in data["today_interview"]:
                lines.append(f"{item['company']}-{item['title']}-{item['name']}-{item['time']}")
            lines.append("")
        if data["tomorrow_interview"]:
            lines.append("明日面试：")
            for item in data["tomorrow_interview"]:
                lines.append(f"{item['company']}-{item['title']}-{item['name']}-{item['time']}")
            lines.append("")
        if data["interview_no_feedback"]:
            lines.append("面试还未反馈：")
            for item in data["interview_no_feedback"]:
                lines.append(f"{item['company']}-{item['title']}-{item['name']}-{item['time']}")
        return "\n".join(lines).rstrip() + "\n"
