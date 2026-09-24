"""生成式 AI 多连接配置模型（内存态，含 SecretStr）。

连接数量不设上限；列表顺序即全局主备顺序——每个业务请求只取顺序最靠前的
两个可用目标（主+备），其余连接作为候补，主备被停用/冷却时才顺位前移。

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
    # 每个角色槽位的思考强度（`low` / `high` / `max`，键是角色、值是档位）。
    # **配了强度就等于要求该模型思考**：可切换思考的模型会被显式打开思考，
    # 否则上游会拒绝 `reasoning_effort`（见 `router._with_target_effort`）。
    # 留空表示跟随模型默认；声明 `prefer_off` 的任务（查询解析）不读这里。
    reasoning_efforts: dict[ModelRole, str] = Field(default_factory=dict)
    probed_roles: frozenset[ModelRole] = frozenset()
    # 上次探测的口径版本。探测口径升级后旧结果不再算数，保存时必须重探，
    # 否则用户会一直看到按旧口径算出的「可用」。
    probe_version: int | None = None
    # 上次探测的能力矩阵（auth/text/json/reasoning/vision → 是否通过）。
    # 只落盘事实，界面据此显示「到底哪一项通过」，而不是只显示一个「可用」。
    probed_capabilities: dict[str, bool] = Field(default_factory=dict)
    enabled: bool = True


class AiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    connections: list[AiConnection] = Field(default_factory=list)
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
    reasoning_efforts: dict[ModelRole, str] = Field(default_factory=dict)
    probed_roles: frozenset[ModelRole] = frozenset()
    probe_version: int | None = None
    probed_capabilities: dict[str, bool] = Field(default_factory=dict)
    enabled: bool = True


class AiConfigPublicView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    connections: list[AiConnectionPublic] = Field(default_factory=list)
    catalog_version: int = 1
