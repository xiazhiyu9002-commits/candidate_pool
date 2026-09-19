from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.backup.service import BackupService
from kerui_recruit.daily_followup.service import SHANGHAI, DailyFollowupService
from kerui_recruit.mail.ingest import MailIngestService
from kerui_recruit.mail.resume_gate import ResumeGate
from kerui_recruit.match.service import MatchService
from kerui_recruit.reminders.mail_service import ReminderMailService
from kerui_recruit.reminders.service import ReminderService


@dataclass(frozen=True, slots=True)
class ReverseMatch:
    jd_id: str
    revision_id: str
    company: str
    title: str
    score: float


class SchedulerService:
    """Background automation: reminder checks and mail polling.

    Runs a lightweight periodic loop (no external scheduler dependency) that
    surfaces due reminders and ingests resumes from the agent mailbox. Passive
    matching is triggered on ingest instead of by this loop. Individual jobs
    never crash the loop.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        match_service: MatchService | None,
        reminder_service: ReminderService | None,
        mail_ingest_service: MailIngestService | None = None,
        mail_auto_sync: bool = False,
        reminder_mail_service: ReminderMailService | None = None,
        backup_service: BackupService | None = None,
        sender_domains: set[str] | None = None,
        resume_gate: ResumeGate | None = None,
        daily_followup_service: DailyFollowupService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.match_service = match_service
        self.reminder_service = reminder_service
        self.mail_ingest_service = mail_ingest_service
        self.mail_auto_sync = mail_auto_sync
        self.reminder_mail_service = reminder_mail_service
        self.backup_service = backup_service
        self.sender_domains = sender_domains
        self.resume_gate = resume_gate
        self.daily_followup_service = daily_followup_service

    async def reverse_match_candidate(
        self, candidate_id: str, *, limit: int = 20, mode: str = "hybrid"
    ) -> list[ReverseMatch]:
        if self.match_service is None:
            return []
        records = await self.match_service.reverse_match_candidate(
            candidate_id, limit=limit, mode=mode
        )
        return [
            ReverseMatch(
                jd_id=record.jd_id,
                revision_id=record.revision_id,
                company=record.company,
                title=record.title,
                score=(record.score.total if record.score is not None
                       else self.match_service.score(record.revision_id, record.hit).total),
            )
            for record in records
        ]

    def due_reminders(self) -> list:
        if self.reminder_service is None:
            return []
        return self.reminder_service.list_due()

    def poll_mail(self) -> list[str]:
        """Ingest resumes from the agent mailbox. Returns new revision ids."""
        if self.mail_ingest_service is None:
            return []
        return self.mail_ingest_service.poll_and_ingest(
            sender_domains=self.sender_domains,
            resume_gate=self.resume_gate,
        )

    def send_reminder_mail(self) -> list[str]:
        """Send due reminders as email. Returns sent reminder ids."""
        if self.reminder_mail_service is None:
            return []
        return self.reminder_mail_service.send_due_reminders()

    def backup_tick(self) -> None:
        """Create a daily snapshot (once per UTC day) and apply retention."""
        if self.backup_service is None:
            return
        today = datetime.now(timezone.utc).date()
        has_today = any(
            datetime.fromtimestamp(
                snapshot.stat().st_mtime, tz=timezone.utc
            ).date() == today
            for snapshot in self.backup_service.backup_dir.glob("backup_*.sqlite3")
        )
        if not has_today:
            self.backup_service.create_snapshot(label="daily")
        self.backup_service.prune()

    def daily_followup_tick(self) -> None:
        """Send due daily followup reports (21:30 evening, 09:00 morning)."""
        if self.daily_followup_service is None:
            return
        self.daily_followup_service.send_due_reports()

    def _next_report_target(self) -> datetime:
        """返回下一个待跟进报告发送时刻（上海时间 naive：09:00 早报 / 21:30 晚报）。"""
        now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
        today = now_sh.date()
        slots = [
            datetime.combine(today, time(9, 0)),
            datetime.combine(today, time(21, 30)),
        ]
        upcoming = [slot for slot in slots if slot > now_sh]
        if upcoming:
            return min(upcoming)
        return datetime.combine(today + timedelta(days=1), time(9, 0))

    async def _daily_followup_loop(self) -> None:
        """精确到发送时刻发送每日待跟进报告，避免 5 分钟轮询带来的延迟。"""
        if self.daily_followup_service is None:
            return
        # 启动先补发一次可能已到点但尚未发送的报告（send_due_reports 内部按日去重）。
        await asyncio.to_thread(self.daily_followup_tick)
        while True:
            target = self._next_report_target()
            now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
            # 略晚于整点醒来，确保 send_due_reports 的时间窗口判断命中。
            delay = max(0.0, (target - now_sh).total_seconds()) + 2.0
            await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(self.daily_followup_tick)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Scheduler job daily_followup (precise) failed: %s", type(error).__name__
                )

    async def run_forever(self, *, interval_seconds: int = 300) -> None:
        followup_task = asyncio.create_task(self._daily_followup_loop())
        try:
            while True:
                operations: list[tuple[str, object]] = [
                    ("reminder_mail", self.send_reminder_mail),
                    ("backup", self.backup_tick),
                ]
                # 只有显式开启自动同步时，后台循环才拉取邮箱；手动 poll_mail 始终可用。
                if self.mail_auto_sync:
                    operations.insert(0, ("mail_ingest", self.poll_mail))
                for name, operation in operations:
                    try:
                        # IMAP/SMTP, SQLite backup and filesystem deletion are all
                        # blocking integrations. Keep them off FastAPI's event loop.
                        await asyncio.to_thread(operation)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        logging.getLogger(__name__).warning(
                            "Scheduler job %s failed: %s", name, type(error).__name__
                        )
                await asyncio.sleep(interval_seconds)
        finally:
            followup_task.cancel()
            with suppress(asyncio.CancelledError):
                await followup_task
