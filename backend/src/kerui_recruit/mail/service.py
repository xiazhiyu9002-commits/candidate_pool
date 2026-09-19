from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import MailCursor, MailProcessRecord


@dataclass(frozen=True, slots=True)
class MailAttachment:
    filename: str
    content: bytes


@dataclass(frozen=True, slots=True)
class MailMessage:
    uid: int
    subject: str
    sender: str
    body: str
    date: str
    attachments: tuple[MailAttachment, ...] = ()


class ImapProvider(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def fetch_unread(self) -> list[MailMessage]: ...

    def fetch_since(self, since_uid: int) -> list[MailMessage]: ...

    def max_uid(self) -> int: ...

    def fetch_by_uid(self, uid: int) -> MailMessage | None: ...

    def mark_read(self, uid: int) -> None: ...

    def uidvalidity(self) -> int | None: ...


class MailService:
    """Idempotent IMAP email fetcher with cursor tracking."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        imap: ImapProvider,
    ) -> None:
        self.session_factory = session_factory
        self.imap = imap

    def sync_mailbox(
        self,
        mailbox: str,
        *,
        subject_filter: str | None = None,
        sender_domains: set[str] | None = None,
    ) -> tuple[list[MailMessage], int | None]:
        """扫描新邮件并记录 pending/ignored，返回 (deliverable, uidvalidity)。

        首次同步拉未读邮件并以当前最大 UID 初始化游标；后续同步按 UID 区间拉取
        新邮件（不区分已读/未读），避免简历邮件被其他客户端标记已读后漏掉。
        只推进扫描游标、不标已读、不决定投递结果；标已读由 ingest 成功入库后
        调用 mark_delivered 完成。
        """
        with self.session_factory() as session:
            cursor = session.query(MailCursor).filter_by(mailbox=mailbox).one_or_none()

        current_uidvalidity: int | None = None
        self.imap.connect()
        try:
            current_uidvalidity = self.imap.uidvalidity()
            if cursor is None:
                # 首次同步：拉未读邮件，并以当前最大 UID 起步，避免拉全量历史。
                fetched = self.imap.fetch_unread()
                since_uid = self.imap.max_uid()
            else:
                fetched = self.imap.fetch_since(cursor.last_uid)
                since_uid = cursor.last_uid
        finally:
            self.imap.disconnect()

        if subject_filter:
            pattern = subject_filter.lower()
            fetched = [m for m in fetched if pattern in m.subject.lower()]

        allowed = {d.strip().lstrip("@").lower() for d in sender_domains} if sender_domains else None

        deliverable: list[MailMessage] = []
        with self.session_factory() as session, session.begin():
            cursor = session.query(MailCursor).filter_by(mailbox=mailbox).one_or_none()
            if cursor is None:
                cursor = MailCursor(mailbox=mailbox, last_uid=since_uid)
                session.add(cursor)
            if current_uidvalidity is not None:
                cursor.uidvalidity = current_uidvalidity
            for msg in fetched:
                if msg.uid > cursor.last_uid:
                    cursor.last_uid = msg.uid
                record = session.query(MailProcessRecord).filter_by(
                    mailbox=mailbox, uidvalidity=current_uidvalidity, uid=msg.uid
                ).one_or_none()
                if record is None:
                    if allowed is not None and not _matches_sender(msg.sender, allowed):
                        session.add(MailProcessRecord(
                            mailbox=mailbox, uidvalidity=current_uidvalidity,
                            uid=msg.uid, status="ignored"))
                    else:
                        session.add(MailProcessRecord(
                            mailbox=mailbox, uidvalidity=current_uidvalidity,
                            uid=msg.uid, status="pending"))
                        deliverable.append(msg)
                elif record.status == "pending":
                    deliverable.append(msg)
                # succeeded/ignored 跳过；failed 由 retryable_failed_uids 重试。
        return deliverable, current_uidvalidity

    def mark_delivered(self, mailbox: str, uidvalidity: int | None, uid: int) -> None:
        """成功入库后：标已读并记录 succeeded。"""
        self.imap.connect()
        try:
            self.imap.mark_read(uid)
        finally:
            self.imap.disconnect()
        with self.session_factory() as session, session.begin():
            record = self._record(session, mailbox, uidvalidity, uid)
            if record is not None:
                record.status = "succeeded"
                record.last_error = None

    def mark_failed(self, mailbox: str, uidvalidity: int | None, uid: int, error: str) -> None:
        """入库失败：记录 failed + attempts 递增，不标已读，允许重试。"""
        with self.session_factory() as session, session.begin():
            record = self._record(session, mailbox, uidvalidity, uid)
            if record is not None:
                record.status = "failed"
                record.attempts = (record.attempts or 0) + 1
                record.last_error = error[:2000]

    def mark_ignored(self, mailbox: str, uidvalidity: int | None, uid: int) -> None:
        """不含简历的邮件：记录 ignored，不标已读。"""
        with self.session_factory() as session, session.begin():
            record = self._record(session, mailbox, uidvalidity, uid)
            if record is not None:
                record.status = "ignored"

    def fetch_by_uid(self, uid: int) -> MailMessage | None:
        """重新获取指定 UID 的邮件内容，用于 failed 记录的重试。"""
        self.imap.connect()
        try:
            return self.imap.fetch_by_uid(uid)
        finally:
            self.imap.disconnect()

    def retryable_failed_uids(self, mailbox: str, *, max_attempts: int = 3) -> list[tuple[int | None, int]]:
        """返回可重试的 (uidvalidity, uid)：attempts 未达上限的 failed 记录。"""
        with self.session_factory() as session:
            records = session.query(MailProcessRecord).filter(
                MailProcessRecord.mailbox == mailbox,
                MailProcessRecord.status == "failed",
                MailProcessRecord.attempts < max_attempts,
            ).all()
            return [(r.uidvalidity, r.uid) for r in records]

    @staticmethod
    def _record(session, mailbox: str, uidvalidity: int | None, uid: int) -> MailProcessRecord | None:
        return session.query(MailProcessRecord).filter_by(
            mailbox=mailbox, uidvalidity=uidvalidity, uid=uid
        ).one_or_none()

    def reset_cursor(self, mailbox: str) -> None:
        with self.session_factory() as session:
            cursor = session.query(MailCursor).filter_by(mailbox=mailbox).one_or_none()
            if cursor is not None:
                cursor.last_uid = 0
                session.commit()


_EMAIL_RE = re.compile(r"[\w.+-]+@([\w.-]+)")
_FULL_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def _sender_domain(sender: str) -> str:
    match = _EMAIL_RE.search(sender)
    return match.group(1).lower() if match else ""


def _sender_email(sender: str) -> str:
    match = _FULL_EMAIL_RE.search(sender)
    return match.group(0).lower() if match else ""


def _matches_sender(sender: str, allowed: set[str]) -> bool:
    """白名单可同时接受完整邮箱地址或发件域名。"""
    return _sender_email(sender) in allowed or _sender_domain(sender) in allowed
