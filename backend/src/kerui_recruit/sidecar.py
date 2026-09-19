from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import uvicorn
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.core.settings_store import SettingsStore
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.runtime import create_runtime_app


@dataclass(frozen=True, slots=True)
class RuntimeArgs:
    host: str
    port: int
    token: str
    data_root: Path


def parse_runtime_args(arguments: Sequence[str] | None = None) -> RuntimeArgs:
    parser = argparse.ArgumentParser(prog="kerui-recruit-sidecar")
    parser.add_argument("--port", required=True, type=int, choices=range(1024, 65536))
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    if len(parsed.token) < 64:
        parser.error("--token must contain at least 256 bits encoded as hexadecimal")
    return RuntimeArgs(
        host="127.0.0.1",
        port=parsed.port,
        token=parsed.token,
        data_root=parsed.data_root.expanduser().resolve(strict=False),
    )


def _secret(name: str) -> SecretStr | None:
    value = os.environ.get(name)
    return SecretStr(value) if value else None


_SENSITIVE = frozenset(
    {
        "deepseek_api_key",
        "siliconflow_api_key",
        "tavily_api_key",
        "imap_auth_code",
        "smtp_auth_code",
        "text_api_key",
        "vision_api_key",
        "embedding_api_key",
        "rerank_api_key",
    }
)


def _read_stored_settings(data_root: Path) -> dict:
    """Read the settings page's JSON file and decrypt its secrets."""
    config_dir = data_root / "config"
    data = SettingsStore(config_dir / "settings.json").load()
    if not data:
        return {}

    encryption = EncryptionService(key_path=str(config_dir / "encryption.key"))
    result: dict = {}
    for key, value in data.items():
        if key in _SENSITIVE and value:
            try:
                result[key] = encryption.decrypt(value)
            except Exception:
                continue
        else:
            result[key] = value
    return result


def build_settings(options: RuntimeArgs) -> Settings:
    stored = _read_stored_settings(options.data_root)

    def value(stored_key: str, env_name: str, default: str | None = None) -> str | None:
        if stored_key in stored and stored[stored_key]:
            return stored[stored_key]
        return os.environ.get(env_name, default)

    def secret_value(stored_key: str, env_name: str) -> SecretStr | None:
        if stored_key in stored and stored[stored_key]:
            return SecretStr(stored[stored_key])
        return _secret(env_name)

    return Settings(
        data_root=options.data_root,
        session_token=options.token,
        deepseek_api_key=secret_value("deepseek_api_key", "DEEPSEEK_API_KEY"),
        deepseek_base_url=value("deepseek_base_url", "DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com",
        siliconflow_api_key=secret_value("siliconflow_api_key", "SILICONFLOW_API_KEY"),
        siliconflow_base_url=value("siliconflow_base_url", "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1") or "https://api.siliconflow.cn/v1",
        siliconflow_embedding_model=value("siliconflow_embedding_model", "SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3") or "BAAI/bge-m3",
        siliconflow_reranker_model=value("siliconflow_reranker_model", "SILICONFLOW_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3") or "BAAI/bge-reranker-v2-m3",
        siliconflow_embedding_fallback_model=value("siliconflow_embedding_fallback_model", "SILICONFLOW_EMBEDDING_FALLBACK_MODEL", "Pro/BAAI/bge-m3") or "",
        siliconflow_reranker_fallback_model=value("siliconflow_reranker_fallback_model", "SILICONFLOW_RERANKER_FALLBACK_MODEL", "Pro/BAAI/bge-reranker-v2-m3") or "",
        siliconflow_text_model=value("siliconflow_text_model", "SILICONFLOW_TEXT_MODEL", "deepseek-ai/DeepSeek-V3") or "deepseek-ai/DeepSeek-V3",
        siliconflow_vision_model=value("siliconflow_vision_model", "SILICONFLOW_VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct") or "Qwen/Qwen2.5-VL-72B-Instruct",
        text_base_url=value("text_base_url", "TEXT_BASE_URL"),
        text_model=value("text_model", "TEXT_MODEL"),
        text_api_key=secret_value("text_api_key", "TEXT_API_KEY"),
        vision_base_url=value("vision_base_url", "VISION_BASE_URL"),
        vision_model=value("vision_model", "VISION_MODEL"),
        vision_api_key=secret_value("vision_api_key", "VISION_API_KEY"),
        embedding_base_url=value("embedding_base_url", "EMBEDDING_BASE_URL"),
        embedding_model=value("embedding_model", "EMBEDDING_MODEL"),
        embedding_api_key=secret_value("embedding_api_key", "EMBEDDING_API_KEY"),
        rerank_base_url=value("rerank_base_url", "RERANK_BASE_URL"),
        rerank_model=value("rerank_model", "RERANK_MODEL"),
        rerank_api_key=secret_value("rerank_api_key", "RERANK_API_KEY"),
        tavily_api_key=secret_value("tavily_api_key", "TAVILY_API_KEY"),
        tavily_base_url=value("tavily_base_url", "TAVILY_BASE_URL", "https://api.tavily.com") or "https://api.tavily.com",
        imap_host=value("imap_host", "IMAP_HOST"),
        imap_account=value("imap_account", "IMAP_ACCOUNT"),
        imap_auth_code=secret_value("imap_auth_code", "IMAP_AUTH_CODE"),
        imap_whitelist=value("imap_whitelist", "IMAP_WHITELIST"),
        smtp_host=value("smtp_host", "SMTP_HOST"),
        smtp_port=_int_value(stored, "smtp_port", os.environ.get("SMTP_PORT"), 465),
        smtp_account=value("smtp_account", "SMTP_ACCOUNT"),
        smtp_auth_code=secret_value("smtp_auth_code", "SMTP_AUTH_CODE"),
        smtp_ssl=_bool_value(stored, "smtp_ssl", os.environ.get("SMTP_SSL"), True),
        reminder_to=value("reminder_to", "REMINDER_TO"),
        daily_followup_enabled=_bool_value(stored, "daily_followup_enabled", os.environ.get("DAILY_FOLLOWUP_ENABLED"), True),
    )


def _int_value(stored: dict, key: str, env: object, default: int) -> int:
    if key in stored and stored[key] is not None:
        return int(stored[key])
    if env is not None:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    return default


def _bool_value(stored: dict, key: str, env: object, default: bool) -> bool:
    if key in stored and stored[key] is not None:
        return bool(stored[key])
    if env is not None:
        return str(env).lower() in ("1", "true", "yes", "on")
    return default


def main(arguments: Sequence[str] | None = None) -> None:
    options = parse_runtime_args(arguments)
    settings = build_settings(options)
    uvicorn.run(
        create_runtime_app(settings),
        host=options.host,
        port=options.port,
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
