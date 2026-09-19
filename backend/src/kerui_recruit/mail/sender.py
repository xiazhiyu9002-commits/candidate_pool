from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


class MailSender:
    """Send plain-text mail over SMTP."""

    def __init__(
        self,
        *,
        host: str,
        account: str,
        password: str,
        port: int = 465,
        ssl: bool = True,
    ) -> None:
        self.host = host
        self.account = account
        self.password = password
        self.port = port
        self.ssl = ssl

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(subject, "utf-8")
        message["From"] = formataddr((str(Header(self.account, "utf-8")), self.account))
        message["To"] = to

        if self.ssl:
            server = smtplib.SMTP_SSL(self.host, self.port)
        else:
            server = smtplib.SMTP(self.host, self.port)
        try:
            server.login(self.account, self.password)
            server.sendmail(self.account, [to], message.as_string())
        finally:
            server.quit()
