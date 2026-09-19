"""生成式 AI 双连接配置模型（内存态，含 SecretStr）。

磁盘态使用 ``encrypted_api_key``（见 ``config_store``），内存态使用 ``SecretStr``。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from kerui_recruit.providers.ai.contracts import ModelRole


class AiConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    provider_id: str
    display_name: str
    api_key: SecretStr
    base_url_override: str | None = None
    parameter_style: str | None = None
    models: dict[ModelRole, str] = Field(default_factory=dict)
    probed_roles: frozenset[ModelRole] = frozenset()
    enabled: bool = True


class AiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    connections: list[AiConnection] = Field(default_factory=list, max_length=2)
    catalog_version: int = 1

    @model_validator(mode="after")
    def _unique_connection_ids(self) -> "AiProviderConfig":
        ids = [connection.connection_id for connection in self.connections]
        if len(ids) != len(set(ids)):
            raise ValueError("connection IDs must be unique")
        return self


class AiConnectionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    provider_id: str
    display_name: str
    masked_api_key: str
    has_api_key: bool
    base_url_override: str | None = None
    parameter_style: str | None = None
    models: dict[ModelRole, str] = Field(default_factory=dict)
    probed_roles: frozenset[ModelRole] = frozenset()
    enabled: bool = True


class AiConfigPublicView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    connections: list[AiConnectionPublic] = Field(default_factory=list)
    catalog_version: int = 1
