"""生成式 AI 配置存储：加密、原子保存、旧设置迁移与脱敏投影。

磁盘文件 ``data_root/config/ai-providers.json``；API Key 用 AES-256-GCM 加密。
``public_view()`` 绝不返回明文或密文 Key。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr

from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.config_models import (
    AiConfigPublicView,
    AiConnection,
    AiConnectionPublic,
    AiProviderConfig,
)
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.validation import validate_connection as _validate_connection

# 旧 settings.json 中参与生成式 AI 的敏感字段（需要解密后才能迁移）。
_LEGACY_SENSITIVE = frozenset({"text_api_key", "deepseek_api_key", "siliconflow_api_key", "vision_api_key"})


class _StoredConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    provider_id: str
    display_name: str
    encrypted_api_key: str
    base_url_override: str | None = None
    parameter_style: str | None = None
    models: dict[str, str] = {}
    probed_roles: list[str] = []
    enabled: bool = True


class _StoredConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    connections: list[_StoredConnection] = []
    catalog_version: int = 1


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}****{secret[-4:]}"


class AiConfigStore:
    def __init__(
        self,
        path: Path,
        encryption,
        catalog_service: CatalogService,
        *,
        allow_insecure_loopback: bool = False,
        legacy_settings_path: Path | None = None,
    ) -> None:
        self.path = path
        self.encryption = encryption
        self.catalog_service = catalog_service
        self.allow_insecure_loopback = allow_insecure_loopback
        # 旧 settings.json 路径；None 表示不迁移。
        self.legacy_settings_path = legacy_settings_path

    def load(self) -> AiProviderConfig:
        if not self.path.exists():
            return AiProviderConfig()
        try:
            stored = _StoredConfig.model_validate_json(self.path.read_text(encoding="utf-8"))
        except Exception:
            return AiProviderConfig()
        connections: list[AiConnection] = []
        for item in stored.connections:
            try:
                api_key = self.encryption.decrypt(item.encrypted_api_key)
            except Exception:
                continue
            connections.append(AiConnection(
                connection_id=item.connection_id,
                provider_id=item.provider_id,
                display_name=item.display_name,
                api_key=SecretStr(api_key),
                base_url_override=item.base_url_override,
                parameter_style=item.parameter_style,
                models={ModelRole(k): v for k, v in item.models.items()},
                probed_roles=frozenset(ModelRole(r) for r in item.probed_roles),
                enabled=item.enabled,
            ))
        return AiProviderConfig(
            schema_version=stored.schema_version,
            connections=connections,
            catalog_version=stored.catalog_version,
        )

    def save(self, config: AiProviderConfig) -> None:
        self._validate(config)
        stored = _StoredConfig(
            schema_version=config.schema_version,
            connections=[
                _StoredConnection(
                    connection_id=connection.connection_id,
                    provider_id=connection.provider_id,
                    display_name=connection.display_name,
                    encrypted_api_key=self.encryption.encrypt(connection.api_key.get_secret_value()),
                    base_url_override=connection.base_url_override,
                    parameter_style=connection.parameter_style,
                    models={role.value: model for role, model in connection.models.items()},
                    probed_roles=[role.value for role in connection.probed_roles],
                    enabled=connection.enabled,
                )
                for connection in config.connections
            ],
            catalog_version=config.catalog_version,
        )
        self._atomic_write(stored.model_dump_json(indent=2))

    def public_view(self) -> AiConfigPublicView:
        config = self.load()
        return AiConfigPublicView(
            schema_version=config.schema_version,
            connections=[
                AiConnectionPublic(
                    connection_id=connection.connection_id,
                    provider_id=connection.provider_id,
                    display_name=connection.display_name,
                    masked_api_key=_mask(connection.api_key.get_secret_value()),
                    has_api_key=bool(connection.api_key.get_secret_value()),
                    base_url_override=connection.base_url_override if connection.provider_id == "custom_openai" else None,
                    parameter_style=connection.parameter_style,
                    models=connection.models,
                    probed_roles=connection.probed_roles,
                    enabled=connection.enabled,
                )
                for connection in config.connections
            ],
            catalog_version=config.catalog_version,
        )

    def migrate_legacy(self) -> AiProviderConfig:
        """幂等迁移旧生成式 AI 设置（读 settings.json），返回内存态配置；不删除任何旧字段。"""
        return self._migrate(self._decrypted_legacy())

    def migrate_from_settings(self, settings) -> AiProviderConfig:
        """从已解密的 Settings 对象迁移（运行时无需依赖 settings.json）。"""
        legacy = {
            "text_api_key": settings.text_api_key.get_secret_value() if settings.text_api_key else None,
            "text_base_url": settings.text_base_url,
            "text_model": settings.text_model,
            "deepseek_api_key": settings.deepseek_api_key.get_secret_value() if settings.deepseek_api_key else None,
            "deepseek_base_url": settings.deepseek_base_url,
            "deepseek_model": settings.deepseek_model,
            "siliconflow_api_key": settings.siliconflow_api_key.get_secret_value() if settings.siliconflow_api_key else None,
            "siliconflow_base_url": settings.siliconflow_base_url,
            "siliconflow_text_model": settings.siliconflow_text_model,
            "vision_api_key": settings.vision_api_key.get_secret_value() if settings.vision_api_key else None,
            "vision_base_url": settings.vision_base_url,
            "vision_model": settings.vision_model,
        }
        return self._migrate({key: value for key, value in legacy.items() if value})

    def _migrate(self, legacy: dict) -> AiProviderConfig:
        if not legacy:
            return AiProviderConfig()

        catalog = self.catalog_service.load()
        connections: list[AiConnection] = []
        seen_keys: list[str] = []

        text_key = legacy.get("text_api_key")
        if text_key:
            connections.append(self._connection(
                connection_id="legacy-text",
                provider_id="custom_openai",
                display_name="自定义（旧文本）",
                api_key=text_key,
                base_url=legacy.get("text_base_url"),
                fast_model=legacy.get("text_model"),
            ))
            seen_keys.append(text_key)

        deepseek_key = legacy.get("deepseek_api_key")
        if deepseek_key and deepseek_key not in seen_keys and len(connections) < 2:
            connections.append(self._connection(
                connection_id="legacy-deepseek",
                provider_id="deepseek",
                display_name="DeepSeek",
                api_key=deepseek_key,
                base_url=legacy.get("deepseek_base_url"),
                fast_model=legacy.get("deepseek_model"),
            ))
            seen_keys.append(deepseek_key)

        siliconflow_key = legacy.get("siliconflow_api_key")
        if siliconflow_key and siliconflow_key not in seen_keys and len(connections) < 2:
            connections.append(self._connection(
                connection_id="legacy-siliconflow",
                provider_id="siliconflow",
                display_name="硅基流动（旧文本）",
                api_key=siliconflow_key,
                base_url=legacy.get("siliconflow_base_url"),
                fast_model=legacy.get("siliconflow_text_model"),
            ))
            seen_keys.append(siliconflow_key)

        # 视觉配置合并到相同 Key 的连接；无法合并时若仍有空位则作为视觉连接。
        vision_key = legacy.get("vision_api_key")
        vision_model = legacy.get("vision_model")
        if vision_key and vision_model:
            matched = next((c for c in connections if c.api_key.get_secret_value() == vision_key), None)
            if matched is not None:
                matched.models[ModelRole.VISION] = vision_model
            elif len(connections) < 2:
                connections.append(self._connection(
                    connection_id="legacy-vision",
                    provider_id="custom_openai",
                    display_name="视觉（旧配置）",
                    api_key=vision_key,
                    base_url=legacy.get("vision_base_url"),
                    vision_model=vision_model,
                ))

        return AiProviderConfig(
            connections=connections,
            catalog_version=catalog.version,
        )

    def validate_connection(self, connection: AiConnection) -> None:
        """对单个连接执行统一校验；违规抛出 ``ValueError``。"""
        _validate_connection(
            connection,
            self.catalog_service.load(),
            allow_insecure_loopback=self.allow_insecure_loopback,
        )

    def _validate(self, config: AiProviderConfig) -> None:
        for connection in config.connections:
            self.validate_connection(connection)

    def _connection(
        self,
        *,
        connection_id: str,
        provider_id: str,
        display_name: str,
        api_key: str,
        base_url: str | None = None,
        fast_model: str | None = None,
        vision_model: str | None = None,
    ) -> AiConnection:
        models: dict[ModelRole, str] = {}
        if fast_model:
            models[ModelRole.FAST_TEXT] = fast_model
        if vision_model:
            models[ModelRole.VISION] = vision_model
        return AiConnection(
            connection_id=connection_id,
            provider_id=provider_id,
            display_name=display_name,
            api_key=SecretStr(api_key),
            base_url_override=base_url if provider_id == "custom_openai" else None,
            models=models,
            probed_roles=frozenset(models),
            enabled=True,
        )

    def _decrypted_legacy(self) -> dict:
        if self.legacy_settings_path is None:
            return {}
        from kerui_recruit.core.settings_store import SettingsStore

        raw = SettingsStore(self.legacy_settings_path).load()
        result: dict = {}
        for key, value in raw.items():
            if key in _LEGACY_SENSITIVE and value:
                try:
                    result[key] = self.encryption.decrypt(value)
                except Exception:
                    continue
            else:
                result[key] = value
        return result

    def _atomic_write(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if self.path.exists():
            bak = self.path.with_suffix(".bak")
            os.replace(self.path, bak)
        os.replace(tmp, self.path)
