import imaplib
import smtplib

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import MailCursor
from kerui_recruit.mail.sender import MailSender
from kerui_recruit.providers.vendors import VENDORS


router = APIRouter(prefix="/api/settings", tags=["settings"])

# 旧生成式 AI 字段：新设置页不再写入；被修改时返回 E_AI_SETTINGS_MOVED。
_GENERATION_FIELDS = frozenset({
    "deepseek_api_key", "deepseek_base_url",
    "text_base_url", "text_model", "text_api_key",
    "vision_base_url", "vision_model", "vision_api_key",
})


class UpdateSettingsRequest(BaseModel):
    deepseek_api_key: str | None = None
    deepseek_base_url: str | None = None
    siliconflow_api_key: str | None = None
    siliconflow_base_url: str | None = None
    siliconflow_embedding_model: str | None = None
    siliconflow_reranker_model: str | None = None
    siliconflow_embedding_fallback_model: str | None = None
    siliconflow_reranker_fallback_model: str | None = None
    text_base_url: str | None = None
    text_model: str | None = None
    text_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    rerank_base_url: str | None = None
    rerank_model: str | None = None
    rerank_api_key: str | None = None
    tavily_api_key: str | None = None
    tavily_base_url: str | None = None
    serpapi_api_key: str | None = None
    serpapi_base_url: str | None = None
    imap_host: str | None = None
    imap_account: str | None = None
    imap_auth_code: str | None = None
    imap_whitelist: str | None = None
    mail_auto_sync: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_account: str | None = None
    smtp_auth_code: str | None = None
    smtp_ssl: bool | None = None
    reminder_to: str | None = None
    daily_followup_enabled: bool | None = None


@router.get("")
def get_settings(request: Request) -> dict:
    services: AppServices = request.app.state.services
    return services.settings_service.get_masked()


@router.get("/vendors")
def list_vendors() -> list[dict]:
    return [
        {
            "key": vendor.key,
            "label": vendor.label,
            "base_url": vendor.base_url,
            "text_model": vendor.text_model,
            "vision_model": vendor.vision_model,
            "embedding_model": vendor.embedding_model,
            "rerank_model": vendor.rerank_model,
            "deprecated": vendor.deprecated,
        }
        for vendor in VENDORS.values()
    ]


@router.put("")
def update_settings(command: UpdateSettingsRequest, request: Request) -> dict:
    services: AppServices = request.app.state.services
    payload = command.model_dump(exclude_none=True)
    masked = services.settings_service.get_masked()
    for key in _GENERATION_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        if value != masked.get(key):
            raise ApiError(422, "E_AI_SETTINGS_MOVED", "生成式 AI 配置请前往「AI 服务」设置")
        payload.pop(key)
    services.settings_service.update(payload)
    return services.settings_service.get_masked()


@router.post("/mail/test")
def test_mail(request: Request) -> dict:
    """Probe IMAP/SMTP connectivity with the currently configured credentials."""
    services: AppServices = request.app.state.services
    # Reuse startup precedence: latest persisted values, then environment.
    from kerui_recruit.sidecar import RuntimeArgs, build_settings

    settings = build_settings(RuntimeArgs(
        host="127.0.0.1", port=0,
        token=services.settings.session_token.get_secret_value(),
        data_root=services.settings.data_root,
    ))
    results: dict[str, dict] = {}

    if settings.mail_enabled:
        try:
            client = imaplib.IMAP4_SSL(settings.imap_host, 993, timeout=15)
            try:
                client.login(settings.imap_account, settings.imap_auth_code.get_secret_value())
                client.select("INBOX")
            finally:
                try:
                    client.logout()
                except Exception:
                    client.shutdown()
            results["imap"] = {"ok": True, "message": "IMAP 连接成功"}
        except Exception as error:
            results["imap"] = {"ok": False, "message": f"IMAP 连接失败：{error}"}
    else:
        results["imap"] = {"ok": False, "message": "未配置 IMAP"}

    smtp_ready = bool(settings.smtp_host and settings.smtp_account and settings.smtp_auth_code)
    if smtp_ready:
        try:
            server = (
                smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
                if settings.smtp_ssl
                else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            )
            try:
                server.login(settings.smtp_account, settings.smtp_auth_code.get_secret_value())  # type: ignore[union-attr]
            finally:
                try:
                    server.quit()
                except Exception:
                    server.close()
            results["smtp"] = {"ok": True, "message": "SMTP 连接成功"}
        except Exception as error:
            results["smtp"] = {"ok": False, "message": f"SMTP 连接失败：{error}"}
    else:
        results["smtp"] = {"ok": False, "message": "未配置 SMTP"}

    return results


@router.post("/mail/sync")
def sync_mail(request: Request) -> dict:
    services: AppServices = request.app.state.services
    scheduler = services.scheduler_service
    if scheduler is None or scheduler.mail_ingest_service is None:
        raise ApiError(503, "E_MAIL_UNAVAILABLE", "邮箱未配置，无法同步")
    try:
        revision_ids = scheduler.poll_mail()
    except Exception as error:
        raise ApiError(502, "E_MAIL_SYNC_FAILED", f"同步失败：{error}") from error
    return {"ingested": len(revision_ids), "revision_ids": revision_ids}


@router.get("/mail/status")
def mail_status(request: Request) -> dict:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        cursor = session.query(MailCursor).filter_by(mailbox="INBOX").one_or_none()
    return {
        "configured": services.settings.mail_enabled,
        "last_uid": cursor.last_uid if cursor is not None else 0,
    }


_MAIL_CONFIRMATION_BODY = """您的邮件功能已成功绑定，现在可以测试以下两个功能：

【邮件简历入库】
1. 从已加入白名单的发件邮箱，向本收件邮箱发送一封带简历附件的邮件；
2. 附件支持 PDF、DOC、DOCX，主题建议写明应聘岗位，正文简单介绍求职意向；
3. 系统每 5 分钟自动拉取一次收件箱，也可在「设置 → 邮箱」点「立即同步」手动触发；
4. 成功入库的简历会出现在「人才库」页面。

【提醒功能】
1. 在「流程中」页面进入某个招聘案例，为其添加提醒（时间与内容）；
2. 到达提醒时间后，系统会向您配置的「提醒收件人邮箱」自动发送提醒邮件；
3. 30 分钟内到期的多条提醒会合并为一封邮件发送。
"""


@router.post("/mail/send-confirmation")
def send_mail_confirmation(request: Request) -> dict:
    services: AppServices = request.app.state.services
    if services.settings_service is None:
        raise ApiError(503, "E_MAIL_UNAVAILABLE", "设置服务不可用")
    smtp = services.settings_service.get_mail_smtp_plain()
    if smtp is None:
        raise ApiError(400, "E_MAIL_NOT_CONFIGURED", "SMTP 未配置，无法发送确认邮件")
    try:
        sender = MailSender(
            host=smtp["host"],
            account=smtp["account"],
            password=smtp["auth_code"],
            port=smtp["port"],
            ssl=smtp["ssl"],
        )
        sender.send(to=smtp["to"], subject="邮件功能已绑定", body=_MAIL_CONFIRMATION_BODY)
        return {"sent": True, "to": smtp["to"], "message": "确认邮件已发送"}
    except Exception as error:
        raise ApiError(502, "E_MAIL_SEND_FAILED", f"确认邮件发送失败：{error}")


@router.post("/mail/send-followup-test")
def send_followup_test(request: Request) -> dict:
    """立即发送一封「每日待跟进候选人」测试邮件（绕过定时与去重门槛）。"""
    services: AppServices = request.app.state.services
    scheduler = services.scheduler_service
    if scheduler is None or scheduler.daily_followup_service is None:
        raise ApiError(503, "E_MAIL_UNAVAILABLE", "每日待跟进报告未启用或 SMTP 未配置")
    from datetime import datetime

    from kerui_recruit.daily_followup.service import SHANGHAI

    service = scheduler.daily_followup_service
    now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
    data = service.gather(now_sh)
    body = service._build_email(data)
    if body is None:
        return {"sent": False, "to": service.to, "message": "当前没有待跟进的候选人，未发送"}
    try:
        service.mail_sender.send(to=service.to, subject="每日待跟进候选人", body=body)
        return {"sent": True, "to": service.to, "message": "测试报告已发送"}
    except Exception as error:
        raise ApiError(502, "E_MAIL_SEND_FAILED", f"测试报告发送失败：{error}")
