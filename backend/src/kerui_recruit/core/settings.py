from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from kerui_recruit.core.paths import AppPaths


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_root: Path
    session_token: SecretStr

    # External AI providers. When both keys are absent the runtime uses
    # deterministic local providers so the app works fully offline.
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_vision_model: str = "deepseek-v4-flash-vision-exp"
    siliconflow_api_key: SecretStr | None = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_embedding_model: str = "BAAI/bge-m3"
    siliconflow_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # 备份通道：与主模型同 key、同 base_url、同 payload，只差 model 一个字段，用于绕过
    # 单个通道的偶发抖动（实测上游会间歇性 429 且不下发 Retry-After）。留空即关闭备份。
    # embedding 的备份必须是同一向量空间的同模型通道（Pro 版实测 cos≈1.0000），
    # 换成别的模型需要整体重建索引，不能当备份用。
    siliconflow_embedding_fallback_model: str = "Pro/BAAI/bge-m3"
    siliconflow_reranker_fallback_model: str = "Pro/BAAI/bge-reranker-v2-m3"
    siliconflow_text_model: str = "deepseek-ai/DeepSeek-V3"
    siliconflow_vision_model: str = "Qwen/Qwen2.5-VL-72B-Instruct"

    # 按能力路由的供应商配置（可选）。缺省时回退到上面对应的聚合 key，
    # 使文本、视觉、Embedding、Rerank 可以分别使用不同供应商。
    text_base_url: str | None = None
    text_model: str | None = None
    text_api_key: SecretStr | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: SecretStr | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None
    rerank_base_url: str | None = None
    rerank_model: str | None = None
    rerank_api_key: SecretStr | None = None

    # Optional web search provider for BD lead discovery. Tavily is preferred;
    # SerpApi is a drop-in alternative.
    tavily_api_key: SecretStr | None = None
    tavily_base_url: str = "https://api.tavily.com"
    serpapi_api_key: SecretStr | None = None
    serpapi_base_url: str = "https://serpapi.com"

    # Optional agent mailbox for passive resume ingestion.
    imap_host: str | None = None
    imap_account: str | None = None
    imap_auth_code: SecretStr | None = None
    imap_whitelist: str | None = None  # 逗号分隔的发件人域名白名单
    # 是否自动后台拉取邮箱简历；默认关闭。关闭后连接测试、状态查询、手动同步仍可用。
    mail_auto_sync: bool = False

    # Optional SMTP for sending reminders.
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_account: str | None = None
    smtp_auth_code: SecretStr | None = None
    smtp_ssl: bool = True
    reminder_to: str | None = None

    # 每日待跟进报告（整理推荐未反馈/明日面试/面试未反馈），默认开启。
    daily_followup_enabled: bool = True

    @property
    def paths(self) -> AppPaths:
        return AppPaths.from_root(self.data_root)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key or self.text_api_key or self.vision_api_key or self.siliconflow_api_key)

    @property
    def search_providers_enabled(self) -> bool:
        return bool(self.siliconflow_api_key or self.embedding_api_key or self.rerank_api_key)

    @property
    def bd_search_enabled(self) -> bool:
        return bool(self.tavily_api_key or self.serpapi_api_key)

    @property
    def mail_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_account and self.imap_auth_code)

    @property
    def mail_auto_sync_enabled(self) -> bool:
        return self.mail_auto_sync and self.mail_enabled

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_account and self.smtp_auth_code and self.reminder_to)

    @property
    def imap_whitelist_domains(self) -> set[str] | None:
        if not self.imap_whitelist:
            return None
        return {d.strip().lower() for d in self.imap_whitelist.split(",") if d.strip()}