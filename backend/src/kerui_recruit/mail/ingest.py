from __future__ import annotations

import threading

from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.mail.resume_gate import ResumeGate
from kerui_recruit.mail.service import MailMessage, MailService
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from kerui_recruit.storage.blobs import BlobStore


class MailIngestService:
    """Pull resumes from the agent mailbox and feed them into the ingest pipeline."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        mail_service: MailService | None,
    ) -> None:
        self.session_factory = session_factory
        self.blob_store = blob_store
        self.mail_service = mail_service
        self._poll_lock = threading.Lock()

    def poll_and_ingest(
        self,
        *,
        mailbox: str = "INBOX",
        sender_domains: set[str] | None = None,
        resume_gate: ResumeGate | None = None,
    ) -> list[str]:
        """扫描并投递邮箱简历附件；仅成功入库后才标已读。返回 revision ids。

        加锁串行化：后台自动同步与手动「立即同步」可能并发触发，二者共享同一个
        IMAP 连接，串行执行可避免连接状态竞态（如 ``FETCH illegal in state
        NONAUTH``）。
        """
        with self._poll_lock:
            return self._poll_and_ingest_locked(
                mailbox=mailbox,
                sender_domains=sender_domains,
                resume_gate=resume_gate,
            )

    def _poll_and_ingest_locked(
        self,
        *,
        mailbox: str,
        sender_domains: set[str] | None,
        resume_gate: ResumeGate | None,
    ) -> list[str]:
        if self.mail_service is None:
            return []

        deliverable, uidvalidity = self.mail_service.sync_mailbox(
            mailbox,
            sender_domains=sender_domains,
        )

        # 可重试的 failed 邮件（attempts 未达上限）通过 fetch_by_uid 重新获取并重投。
        to_deliver: list[tuple[int | None, MailMessage]] = [
            (uidvalidity, message) for message in deliverable
        ]
        for retry_uidvalidity, uid in self.mail_service.retryable_failed_uids(mailbox):
            message = self.mail_service.fetch_by_uid(uid)
            if message is not None:
                to_deliver.append((retry_uidvalidity, message))

        ingested: list[str] = []
        for message_uidvalidity, message in to_deliver:
            if resume_gate is not None:
                filenames = [attachment.filename for attachment in message.attachments]
                try:
                    if not resume_gate.is_resume(
                        subject=message.subject,
                        body=message.body,
                        attachment_filenames=filenames,
                    ):
                        # 不含简历的邮件不标已读，记录 ignored 避免重复处理。
                        self.mail_service.mark_ignored(mailbox, message_uidvalidity, message.uid)
                        continue
                except Exception:
                    # 大模型判断失败时不阻断入库，回退到按附件后缀处理。
                    pass
            try:
                for attachment in message.attachments:
                    with self.session_factory() as session:
                        result = ResumeIngestService(session, self.blob_store).ingest(
                            IngestResume(filename=attachment.filename, content=attachment.content)
                        )
                        ingested.append(result.revision_id)
            except Exception as error:
                # 入库失败：不标已读，记录 failed + attempts 递增，允许下次重试。
                self.mail_service.mark_failed(
                    mailbox, message_uidvalidity, message.uid, str(error))
                continue
            # 成功持久化入库后才标已读。
            self.mail_service.mark_delivered(mailbox, message_uidvalidity, message.uid)
        return ingested
