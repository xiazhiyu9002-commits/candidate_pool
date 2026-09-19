from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import MailProcessRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.mail.ingest import MailIngestService
from kerui_recruit.mail.service import ImapProvider, MailAttachment, MailMessage, MailService
from kerui_recruit.resumes.ingest import ResumeIngestService
from kerui_recruit.storage.blobs import BlobStore


class FakeImap(ImapProvider):
    def __init__(
        self,
        messages: list[MailMessage] | None = None,
        *,
        uidvalidity: int | None = None,
    ) -> None:
        self.messages = messages or []
        self.marked_read: list[int] = []
        self.connect_count = 0
        self.disconnect_count = 0
        self._uidvalidity = uidvalidity
        self._unread = {m.uid for m in self.messages}

    def connect(self) -> None:
        self.connect_count += 1

    def disconnect(self) -> None:
        self.disconnect_count += 1

    def fetch_unread(self) -> list[MailMessage]:
        return [m for m in self.messages if m.uid in self._unread]

    def fetch_since(self, since_uid: int) -> list[MailMessage]:
        return [m for m in self.messages if m.uid > since_uid]

    def max_uid(self) -> int:
        return max((m.uid for m in self.messages), default=0)

    def fetch_by_uid(self, uid: int) -> MailMessage | None:
        for m in self.messages:
            if m.uid == uid:
                return m
        return None

    def mark_read(self, uid: int) -> None:
        self.marked_read.append(uid)
        self._unread.discard(uid)

    def uidvalidity(self) -> int | None:
        return self._uidvalidity


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_sync_mailbox_scans_without_marking_read(
    session_factory: sessionmaker[Session],
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="Java 工程师", sender="hr@test.com", body="简历", date="2026-01-01"),
            MailMessage(uid=2, subject="前端工程师", sender="hr@test.com", body="简历", date="2026-01-02"),
        ]
    )
    service = MailService(session_factory=session_factory, imap=fake)
    deliverable, uidvalidity = service.sync_mailbox("INBOX")
    assert [m.uid for m in deliverable] == [1, 2]
    assert uidvalidity is None
    # 扫描不标已读：标已读只在 ingest 成功入库后发生。
    assert fake.marked_read == []

    # 第二次扫描：游标已推进到 2，按 UID 区间不再拉到旧邮件（去重靠 cursor + record）。
    deliverable2, _ = service.sync_mailbox("INBOX")
    assert [m.uid for m in deliverable2] == []


def test_sync_mailbox_with_subject_filter(
    session_factory: sessionmaker[Session],
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="Java 工程师", sender="hr@test.com", body="...", date="2026-01-01"),
            MailMessage(uid=2, subject="产品经理", sender="hr@test.com", body="...", date="2026-01-02"),
            MailMessage(uid=3, subject="Java 架构师", sender="hr@test.com", body="...", date="2026-01-03"),
        ]
    )
    service = MailService(session_factory=session_factory, imap=fake)
    deliverable, _ = service.sync_mailbox("INBOX", subject_filter="java")
    assert len(deliverable) == 2
    assert all("Java" in m.subject for m in deliverable)


def test_sync_mailbox_filters_by_sender_domain(
    session_factory: sessionmaker[Session],
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="Java", sender="hr@boss.com", body="...", date="2026-01-01"),
            MailMessage(uid=2, subject="Java", sender="hr@liepin.com", body="...", date="2026-01-02"),
            MailMessage(uid=3, subject="Java", sender="hr@other.com", body="...", date="2026-01-03"),
        ]
    )
    service = MailService(session_factory=session_factory, imap=fake)
    deliverable, _ = service.sync_mailbox("INBOX", sender_domains={"boss.com", "liepin.com"})
    assert {m.uid for m in deliverable} == {1, 2}


def test_sync_mailbox_filters_by_full_email_and_prefixed_domain(
    session_factory: sessionmaker[Session],
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="小号简历", sender="123456789@qq.com", body="...", date="2026-01-01"),
            MailMessage(uid=2, subject="boss通知", sender="hr@notice.bosszhipin.com", body="...", date="2026-01-02"),
            MailMessage(uid=3, subject="其他QQ", sender="other@qq.com", body="...", date="2026-01-03"),
            MailMessage(uid=4, subject="其他", sender="x@y.com", body="...", date="2026-01-04"),
        ]
    )
    service = MailService(session_factory=session_factory, imap=fake)
    deliverable, _ = service.sync_mailbox("INBOX", sender_domains={"123456789@qq.com", "@notice.bosszhipin.com"})
    assert {m.uid for m in deliverable} == {1, 2}


def test_sync_mailbox_records_non_whitelisted_as_ignored_without_marking_read(
    session_factory: sessionmaker[Session],
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="白名单简历", sender="hr@boss.com", body="...", date="2026-01-01"),
            MailMessage(uid=2, subject="营销邮件", sender="spam@other.com", body="...", date="2026-01-02"),
        ]
    )
    service = MailService(session_factory=session_factory, imap=fake)
    deliverable, _ = service.sync_mailbox("INBOX", sender_domains={"boss.com"})

    # 只交付白名单邮件，且不标任何邮件已读（非白名单也不标已读）。
    assert [m.uid for m in deliverable] == [1]
    assert fake.marked_read == []

    # 非白名单邮件记录为 ignored，避免重复处理。
    with session_factory() as session:
        ignored = session.query(MailProcessRecord).filter_by(mailbox="INBOX", uid=2).one()
        assert ignored.status == "ignored"

    # 第二次扫描：游标已推进，按 UID 区间不再拉到旧邮件。
    deliverable2, _ = service.sync_mailbox("INBOX", sender_domains={"boss.com"})
    assert [m.uid for m in deliverable2] == []


def test_decode_filename_handles_rfc2047_resume_name() -> None:
    import base64

    from kerui_recruit.mail.imap_provider import _decode_filename

    encoded = "=?utf-8?B?" + base64.b64encode("简历.pdf".encode("utf-8")).decode() + "?="
    assert _decode_filename(encoded) == "简历.pdf"
    assert _decode_filename("plain.pdf") == "plain.pdf"
    assert _decode_filename(None) == ""


def test_mail_ingest_creates_revision_and_marks_read(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(
                uid=1,
                subject="简历",
                sender="hr@boss.com",
                body="附件简历",
                date="2026-01-01",
                attachments=(MailAttachment(filename="张三.pdf", content=b"%PDF-1.4 resume"),),
            )
        ]
    )
    mail_service = MailService(session_factory=session_factory, imap=fake)
    blob_store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    ingest = MailIngestService(
        session_factory=session_factory,
        blob_store=blob_store,
        mail_service=mail_service,
    )

    revisions = ingest.poll_and_ingest(sender_domains={"boss.com"})

    assert len(revisions) == 1
    assert revisions[0]
    # 成功入库后才标已读。
    assert fake.marked_read == [1]
    with session_factory() as session:
        record = session.query(MailProcessRecord).filter_by(mailbox="INBOX", uid=1).one()
        assert record.status == "succeeded"


def test_mail_ingest_failure_keeps_unread_and_retryable(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch
) -> None:
    fake = FakeImap(
        messages=[
            MailMessage(
                uid=1,
                subject="简历",
                sender="hr@boss.com",
                body="附件简历",
                date="2026-01-01",
                attachments=(MailAttachment(filename="张三.pdf", content=b"%PDF-1.4 resume"),),
            )
        ]
    )
    mail_service = MailService(session_factory=session_factory, imap=fake)
    blob_store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    ingest = MailIngestService(
        session_factory=session_factory,
        blob_store=blob_store,
        mail_service=mail_service,
    )

    def boom(self, item):
        raise RuntimeError("ingest failed")

    monkeypatch.setattr(ResumeIngestService, "ingest", boom)

    revisions = ingest.poll_and_ingest(sender_domains={"boss.com"})

    assert revisions == []
    # 入库失败不标已读。
    assert fake.marked_read == []
    # 记录 failed 且可重试。
    assert mail_service.retryable_failed_uids("INBOX") == [(None, 1)]
    with session_factory() as session:
        record = session.query(MailProcessRecord).filter_by(mailbox="INBOX", uid=1).one()
        assert record.status == "failed"
        assert record.attempts == 1
        assert record.last_error == "ingest failed"


def test_sync_mailbox_fetches_read_new_mail_by_uid_range(
    session_factory: sessionmaker[Session],
) -> None:
    """已读的新邮件也必须被拉到：邮件同步不能依赖「未读」状态。

    简历邮件若先被手机 / 网页客户端标记为已读，旧实现（只搜 UNSEEN）会永久漏掉。
    """
    fake = FakeImap(
        messages=[
            MailMessage(uid=1, subject="旧邮件", sender="hr@boss.com", body="...", date="2026-01-01"),
        ]
    )
    fake._unread = set()  # 模拟全部已读
    service = MailService(session_factory=session_factory, imap=fake)

    # 首次同步：无未读，游标初始化为当前最大 UID = 1。
    deliverable, _ = service.sync_mailbox("INBOX", sender_domains={"boss.com"})
    assert deliverable == []

    # 新邮件 uid=2 到达且已被标记为已读，第二次同步仍应按 UID 区间拉到它。
    fake.messages.append(
        MailMessage(uid=2, subject="新简历", sender="hr@boss.com", body="...", date="2026-01-02")
    )
    deliverable2, _ = service.sync_mailbox("INBOX", sender_domains={"boss.com"})
    assert [m.uid for m in deliverable2] == [2]
