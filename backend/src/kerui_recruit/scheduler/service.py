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
        soft_delete_service=None,
        work_years_rollover=None,
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
        self.soft_delete_service = soft_delete_service
        self.work_years_rollover = work_years_rollover
        # 软删除残留清理每个 UTC 日只跑一次，避免 5 分钟轮询里反复全表扫描。
        self._last_purge_date = None
        # 工作年限/年龄滚动刷新同样每上海日只跑一次（值未变时本就无写入）。
        self._last_years_refresh_date = None

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

    def purge_soft_deleted_tick(self) -> None:
        """回收站过期物理清理（每 UTC 日一次）。

        软删除（候选人/JD 置 ``deleted_at``）是早期行为，现已改为物理删除，
        但回收站里可能仍有历史记录。这里按保留期做物理清理，避免长期堆积
        —— 真实库曾积压 47 个岗位 + 208 个候选人无人清理。

        清理走**单条删除服务**（见 ``SoftDeleteService.purge_expired``），
        因此流程快照与解除关联的行为与手动删除一致。
        """
        if self.soft_delete_service is None:
            return
        today = datetime.now(timezone.utc).date()
        if self._last_purge_date == today:
            return
        self._last_purge_date = today
        removed = self.soft_delete_service.purge_expired()
        if removed:
            logging.getLogger(__name__).info("回收站过期清理：物理删除 %d 条", removed)

    def refresh_work_years_tick(self) -> None:
        """按连续口径滚动刷新工作年限、年龄与画像里的对应数字（每上海日一次）。

        年限按 0.1 精度只在跨月时变化、年龄每年才变，因此绝大多数日次调用直接返回，
        开销接近零。刷新同步候选人列、版本 JSON 与搜索索引。

        **画像不做整段重生**：只就地替换画像里的总年限与年龄两个数字（``resumes/profile_text``），
        因此不需要调用模型、不产生额度消耗，也不会覆盖使用者的措辞。
        """
        if self.work_years_rollover is None:
            return
        today = datetime.now(SHANGHAI).date()
        if self._last_years_refresh_date == today:
            return
        self._last_years_refresh_date = today
        self.work_years_rollover.refresh(today)

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
                    ("purge_soft_deleted", self.purge_soft_deleted_tick),
                    ("refresh_work_years", self.refresh_work_years_tick),
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
