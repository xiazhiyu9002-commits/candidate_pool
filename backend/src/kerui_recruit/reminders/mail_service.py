from __future__ import annotations

from kerui_recruit.db.models import Reminder
from kerui_recruit.mail.sender import MailSender
from kerui_recruit.reminders.service import ReminderService

_MERGE_WINDOW_SECONDS = 1800  # 30 分钟内到期的提醒合并为一封


class ReminderMailService:
    """Send due reminders as email and dismiss them."""

    def __init__(
        self,
        *,
        reminder_service: ReminderService,
        mail_sender: MailSender | None,
        to: str | None,
    ) -> None:
        self.reminder_service = reminder_service
        self.mail_sender = mail_sender
        self.to = to

    def send_due_reminders(self) -> list[str]:
        """Send all due reminders and dismiss them. Returns sent reminder ids."""
        if self.mail_sender is None or not self.to:
            return []

        due = self.reminder_service.list_due()
        sent: list[str] = []
        for group in self._group_by_window(due):
            self._send_group(group)
            for reminder in group:
                self.reminder_service.dismiss(reminder.id)
                sent.append(reminder.id)
        return sent

    def _send_group(self, group: list[Reminder]) -> None:
        if len(group) == 1:
            reminder = group[0]
            subject = reminder.title
            body = reminder.note or reminder.title
        else:
            subject = f"你有 {len(group)} 条待办提醒"
            body = "\n\n".join(
                f"{reminder.title}\n{reminder.note or ''}".strip()
                for reminder in group
            )
        self.mail_sender.send(to=self.to, subject=subject, body=body)

    @staticmethod
    def _group_by_window(reminders: list[Reminder]) -> list[list[Reminder]]:
        ordered = sorted(reminders, key=lambda r: r.remind_at)
        groups: list[list[Reminder]] = []
        current: list[Reminder] = []
        previous: Reminder | None = None
        for reminder in ordered:
            if previous is not None and (reminder.remind_at - previous.remind_at).total_seconds() > _MERGE_WINDOW_SECONDS:
                groups.append(current)
                current = []
            current.append(reminder)
            previous = reminder
        if current:
            groups.append(current)
        return groups
