from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from typing import Iterable

from kerui_recruit.mail.service import ImapProvider, MailAttachment, MailMessage

_RESUME_SUFFIXES = (".pdf", ".doc", ".docx")


class ImapLibProvider(ImapProvider):
    """Real IMAP provider backed by the standard library ``imaplib``.

    Fetches unread messages and their resume attachments. The cursor key is the
    server UID so reconnects do not drop or duplicate messages.
    """

    def __init__(
        self,
        *,
        host: str,
        account: str,
        password: str,
        port: int = 993,
        ssl: bool = True,
        mailbox: str = "INBOX",
    ) -> None:
        self.host = host
        self.account = account
        self.password = password
        self.port = port
        self.ssl = ssl
        self.mailbox = mailbox
        self._client: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        if self.ssl:
            self._client = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            self._client = imaplib.IMAP4(self.host, self.port)
        self._client.login(self.account, self.password)
        self._client.select(self.mailbox)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def fetch_unread(self) -> list[MailMessage]:
        """拉取收件箱中的未读邮件（仅首次同步使用）。

        首次同步用 ``UNSEEN`` 拉取未读邮件，避免 ``UID 1:*`` 把大量历史邮件
        逐个 ``FETCH`` 出来导致卡死。后续同步改用 :meth:`fetch_since` 按 UID
        区间拉取新邮件，不依赖未读状态。
        """
        assert self._client is not None
        _, data = self._client.uid("search", None, "UNSEEN")
        return self._fetch_uids(_split_uids(data))

    def fetch_since(self, since_uid: int) -> list[MailMessage]:
        """拉取 UID 大于 ``since_uid`` 的所有邮件（不区分已读/未读）。

        邮件同步不能依赖「未读」状态：简历邮件若先被其他客户端（手机 / 网页）
        标记为已读，就会永久漏掉。这里按 UID 区间拉取新邮件，已读也能处理。
        """
        assert self._client is not None
        _, data = self._client.uid("search", None, f"UID {since_uid + 1}:*")
        return self._fetch_uids(_split_uids(data))

    def max_uid(self) -> int:
        """返回收件箱当前最大 UID（用于首次同步初始化游标，避免拉全量历史）。"""
        assert self._client is not None
        _, data = self._client.uid("search", None, "UID 1:*")
        uids = _split_uids(data)
        return int(uids[-1]) if uids else 0

    def _fetch_uids(self, uids: list[bytes]) -> list[MailMessage]:
        messages: list[MailMessage] = []
        for uid in uids:
            _, msg_data = self._client.uid("fetch", uid, "(RFC822)")
            raw = _first_bytes(msg_data)
            if raw is None:
                continue
            messages.append(_parse_message(int(uid), raw))
        return messages

    def fetch_by_uid(self, uid: int) -> MailMessage | None:
        """重新获取指定 UID 的邮件，用于入库失败后的重试。"""
        assert self._client is not None
        _, msg_data = self._client.uid("fetch", str(uid).encode(), "(RFC822)")
        raw = _first_bytes(msg_data)
        if raw is None:
            return None
        return _parse_message(uid, raw)

    def mark_read(self, uid: int) -> None:
        assert self._client is not None
        self._client.uid("store", str(uid).encode(), "+FLAGS", r"(\Seen)")

    def uidvalidity(self) -> int | None:
        """Return the mailbox UIDVALIDITY, or None when the server omits it.

        Some providers (e.g. QQ Mail) renumber UIDs after deletion; the
        UIDVALIDITY token changes then, letting callers detect stale cursors.
        """
        assert self._client is not None
        raw = self._client.response("UIDVALIDITY")
        if not raw:
            return None
        # Python 3.12 的 imaplib response() 返回 (code, [data]) 元组；
        # 兼容旧版本直接返回 [data] 的形式。
        data = raw[1] if isinstance(raw, tuple) and len(raw) >= 2 else raw
        for item in data:
            text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            match = re.search(r"(\d+)", text)
            if match:
                return int(match.group(1))
        return None


def _split_uids(data: list[bytes] | None) -> list[bytes]:
    return data[0].split() if data and data[0] else []


def _first_bytes(data: Iterable[object]) -> bytes | None:
    for part in data:
        if isinstance(part, tuple):
            payload = part[1]
            if isinstance(payload, bytes):
                return payload
            if isinstance(payload, str):
                return payload.encode("utf-8", "replace")
    return None


def _parse_message(uid: int, raw: bytes) -> MailMessage:
    msg = email.message_from_bytes(raw)
    subject = str(msg.get("Subject", ""))
    sender = str(msg.get("From", ""))
    date = str(msg.get("Date", ""))
    attachments: list[MailAttachment] = []

    body_parts: list[str] = []
    for part in msg.walk():
        content_disposition = part.get_content_disposition()
        filename = _decode_filename(part.get_filename())
        if filename and _is_resume(filename):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                attachments.append(MailAttachment(filename=filename, content=payload))
        elif part.get_content_type() == "text/plain" and content_disposition != "attachment":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                body_parts.append(payload.decode("utf-8", "replace"))

    return MailMessage(
        uid=uid,
        subject=subject,
        sender=sender,
        body="\n".join(body_parts),
        date=date,
        attachments=tuple(attachments),
    )


def _decode_filename(filename: str | None) -> str:
    """解码 RFC 2047 编码的中文附件名（QQ 邮箱常把文件名编码成 =?utf-8?B?...?=）。"""
    if not filename:
        return ""
    try:
        return str(make_header(decode_header(filename)))
    except Exception:
        return filename


def _is_resume(filename: str) -> bool:
    return filename.lower().endswith(_RESUME_SUFFIXES)
