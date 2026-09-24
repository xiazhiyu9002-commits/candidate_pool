"""生成式 AI 安全接口：目录、配置、探测、状态。

密钥只在服务端解密后使用；所有响应只返回脱敏密钥（masked_api_key）与 has_api_key，
绝不返回明文或密文 API Key。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from kerui_recruit.api.errors import ApiError
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.probes import PROBE_SCHEMA_VERSION, probed_roles

router = APIRouter(prefix="/api/ai", tags=["ai"])


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str | None = None
    provider_id: str
    display_name: str
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    probe_token: str | None = None
    base_url_override: str | None = None
    parameter_style: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    # 每个角色槽位的思考强度（角色 → `low`/`high`/`max`）。取值必须落在该角色所用
    # 模型档案声明的 `supported_reasoning_efforts` 里，由 `validation.validate_connection` 兜底。
    reasoning_efforts: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class AiConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: list[ConnectionUpdate] = Field(default_factory=list)


class ConnectionResponse(BaseModel):
    connection_id: str
    provider_id: str
    display_name: str
    masked_api_key: str
    has_api_key: bool
    base_url_override: str | None
    parameter_style: str | None
    models: dict[str, str]
    reasoning_efforts: dict[str, str] = {}
    probed_roles: list[str]
    # 上次探测的能力矩阵与口径版本：界面据此显示「到底哪一项通过」，
    # 并且一旦口径升级（probe_version 落后）就知道这份结果已过期。
    probed_capabilities: dict[str, bool] = {}
    probe_version: int | None = None
    enabled: bool


class AiConfigResponse(BaseModel):
    protection_level: Literal["none", "single", "dual"]
    connections: list[ConnectionResponse]
    catalog_version: int


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    api_key: str = ""
    connection_id: str | None = None
    base_url_override: str | None = None
    parameter_style: str | None = None
    models: dict[str, str] = Field(default_factory=dict)


def _manager(request: Request):
    manager = request.app.state.services.ai_manager
    if manager is None:
        raise ApiError(503, "E_AI_UNAVAILABLE", "AI 服务尚未初始化")
    return manager


def _protection_level(serving_roles: dict[str, frozenset[ModelRole]]) -> str:
    usable = [cid for cid, roles in serving_roles.items() if roles]
    if not usable:
        return "none"
    if len(usable) < 2:
        return "single"
    # 两个连接必须至少有一个角色重叠（该角色真正具备主备），否则不构成“双服务保护”。
    overlap = any(
        role in serving_roles[usable[0]] and role in serving_roles[usable[1]]
        for role in serving_roles[usable[0]]
    )
    return "dual" if overlap else "single"


def _role_models(models: dict[str, str]) -> dict[ModelRole, str]:
    result: dict[ModelRole, str] = {}
    for key, value in models.items():
        try:
            result[ModelRole(key)] = value
        except ValueError:
            raise ApiError(422, "E_AI_INVALID_ROLE", f"未知角色：{key}")
    return result


def _role_efforts(efforts: dict[str, str]) -> dict[ModelRole, str]:
    """角色 → 思考强度。档位白名单校验交给 `validation.validate_connection`（它能看到模型档案）。"""
    result: dict[ModelRole, str] = {}
    for key, value in efforts.items():
        try:
            result[ModelRole(key)] = value
        except ValueError:
            raise ApiError(422, "E_AI_INVALID_ROLE", f"未知角色：{key}")
    return result


def _capability_matrix(report) -> dict[str, bool]:
    """把探测报告压成可落盘、可展示的能力矩阵。"""
    return {
        "auth": report.auth.ok,
        "text": report.text.ok,
        "json": report.json.ok,
        "reasoning": report.reasoning.ok,
        "vision": report.vision.ok,
    }


def _connection_changed(prior: AiConnection, current: AiConnection) -> bool:
    """判断该连接是否需要重新探测。

    返回 True 的两种情况：身份/凭据/路由配置发生变化，或已存的探测结果**口径过期**
    （``probe_version`` 落后于当前 ``PROBE_SCHEMA_VERSION``）。
    """
    return (
        prior.provider_id != current.provider_id
        or prior.api_key.get_secret_value() != current.api_key.get_secret_value()
        or prior.base_url_override != current.base_url_override
        or prior.parameter_style != current.parameter_style
        or prior.models != current.models
        # 探测口径升级后旧结果不再算数：必须重探，否则用户一直看到按旧口径算出的「可用」。
        or prior.probe_version != PROBE_SCHEMA_VERSION
        or prior.enabled != current.enabled  # false→true 启用需重新探测
    )


def _response(manager, public_view) -> AiConfigResponse:
    connections = []
    for item in public_view.connections:
        connections.append(ConnectionResponse(
            connection_id=item.connection_id,
            provider_id=item.provider_id,
            display_name=item.display_name,
            masked_api_key=item.masked_api_key,
            has_api_key=item.has_api_key,
            base_url_override=item.base_url_override,
            parameter_style=item.parameter_style,
            models={role.value: model for role, model in item.models.items()},
            reasoning_efforts={role.value: effort for role, effort in item.reasoning_efforts.items()},
            probed_roles=[role.value for role in item.probed_roles],
            probed_capabilities=dict(item.probed_capabilities),
            probe_version=item.probe_version,
            enabled=item.enabled,
        ))
    return AiConfigResponse(
        protection_level=_protection_level(manager.serving_roles()),
        connections=connections,
        catalog_version=public_view.catalog_version,
    )


@router.get("/catalog")
def get_catalog(request: Request) -> dict:
    manager = _manager(request)
    catalog = manager.catalog()
    return {
        "version": catalog.version,
        "default_provider_id": catalog.default_provider_id,
        "providers": [
            provider.model_dump(mode="json")
            for provider in catalog.providers.values()
        ],
    }


@router.post("/catalog/refresh")
def refresh_catalog(request: Request) -> dict:
    manager = _manager(request)
    result = manager.refresh_catalog()
    return {"status": result.status, "active_version": result.active_version, "message": result.message}


@router.get("/config", response_model=AiConfigResponse)
def get_config(request: Request) -> AiConfigResponse:
    manager = _manager(request)
    return _response(manager, manager.public_view())


@router.put("/config", response_model=AiConfigResponse)
async def put_config(request: Request, update: AiConfigUpdate) -> AiConfigResponse:
    manager = _manager(request)
    existing = {c.connection_id: c for c in manager.config.connections}

    connections: list[AiConnection] = []
    probe_tokens: dict[str, str] = {}
    for item in update.connections:
        if item.connection_id is not None and item.connection_id not in existing:
            raise ApiError(422, "E_AI_UNKNOWN_CONNECTION", f"未知连接：{item.connection_id}")
        if item.connection_id is None and item.api_key is None:
            raise ApiError(422, "E_AI_MISSING_KEY", "新连接必须提供 API Key")

        if item.connection_id is not None and item.api_key is None and not item.clear_api_key:
            prior = existing[item.connection_id]
            if prior.provider_id != item.provider_id:
                raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "复用 Key 时供应商必须与原连接一致")
            if item.provider_id == "custom_openai":
                if prior.base_url_override != item.base_url_override:
                    raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "Base URL 改变时需重新输入 Key")
                if prior.parameter_style != item.parameter_style:
                    raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "兼容风格改变时需重新输入 Key")
            api_key = prior.api_key
        elif item.clear_api_key:
            api_key = SecretStr("")
        else:
            api_key = item.api_key or SecretStr("")

        connection_id = item.connection_id or str(uuid4())
        connections.append(AiConnection(
            connection_id=connection_id,
            provider_id=item.provider_id,
            display_name=item.display_name,
            api_key=api_key,
            base_url_override=item.base_url_override,
            parameter_style=item.parameter_style,
            models=_role_models(item.models),
            reasoning_efforts=_role_efforts(item.reasoning_efforts),
            enabled=item.enabled,
        ))
        if item.probe_token:
            probe_tokens[connection_id] = item.probe_token

    # 保存前校验并对“新增或变更”的连接做真实探测；未变化或携带有效探测凭据的连接不重复请求。
    probed_connections: list[AiConnection] = []
    for connection in connections:
        try:
            manager.validate_connection(connection)
        except ValueError as error:
            raise ApiError(422, "E_AI_INVALID_CONFIG", str(error)) from error
        if not connection.models:
            raise ApiError(422, "E_AI_INVALID_CONFIG", "连接必须至少分配一个模型")
        if not connection.enabled:
            probed_connections.append(connection)
            continue
        prior = existing.get(connection.connection_id)
        if prior is not None and not _connection_changed(prior, connection):
            # Key/供应商/Base URL/风格/模型分配均未变化、且探测口径未过期：复用旧结果，不再请求。
            probed_connections.append(connection.model_copy(update={
                "probed_roles": prior.probed_roles,
                "probe_version": prior.probe_version,
                "probed_capabilities": dict(prior.probed_capabilities),
            }))
            continue
        token = probe_tokens.get(connection.connection_id)
        report = manager.cached_probe(connection, token) if token else None
        if report is None:
            report = await manager.probe(connection)
        probed_roles_result = probed_roles(report, connection)
        if not probed_roles_result:
            raise ApiError(422, "E_AI_UNUSABLE_CONFIG", "连接探测失败：没有可用的能力路由")
        probed_connections.append(connection.model_copy(update={
            "probed_roles": probed_roles_result,
            "probe_version": PROBE_SCHEMA_VERSION,
            "probed_capabilities": _capability_matrix(report),
        }))

    try:
        config = AiProviderConfig(connections=probed_connections, catalog_version=manager.catalog().version)
    except ValueError as error:
        raise ApiError(422, "E_AI_INVALID_CONFIG", str(error)) from error
    try:
        # 热生效：保存 + 原子替换快照（在 manager 内部串行）。
        await manager.update_config(config)
    except ValueError as error:
        raise ApiError(422, "E_AI_INVALID_CONFIG", str(error)) from error
    return _response(manager, manager.public_view())


@router.post("/probe")
async def probe(request: Request, body: ProbeRequest) -> dict:
    manager = _manager(request)
    api_key = body.api_key
    if not api_key and body.connection_id:
        # 已保存连接重新探测：根据 connection_id 安全复用既有 Key，前端不得回传明文 Key。
        existing = {c.connection_id: c for c in manager.config.connections}
        prior = existing.get(body.connection_id)
        if prior is None:
            raise ApiError(422, "E_AI_UNKNOWN_CONNECTION", f"未知连接：{body.connection_id}")
        if prior.provider_id != body.provider_id:
            raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "复用 Key 时供应商必须与原连接一致")
        if body.provider_id == "custom_openai":
            if prior.base_url_override != body.base_url_override:
                raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "Base URL 改变时需重新输入 Key")
            if prior.parameter_style != body.parameter_style:
                raise ApiError(422, "E_AI_KEY_REUSE_MISMATCH", "兼容风格改变时需重新输入 Key")
        api_key = prior.api_key.get_secret_value()
    if not api_key:
        raise ApiError(422, "E_AI_MISSING_KEY", "请提供 API Key")
    connection = AiConnection(
        connection_id=body.connection_id or "probe",
        provider_id=body.provider_id,
        display_name="probe",
        api_key=SecretStr(api_key),
        base_url_override=body.base_url_override,
        parameter_style=body.parameter_style,
        models=_role_models(body.models),
    )
    try:
        manager.validate_connection(connection)
    except ValueError as error:
        raise ApiError(422, "E_AI_INVALID_CONFIG", str(error)) from error
    token, report = await manager.probe_cached(connection)
    return {
        "probe_token": token,
        "auth": asdict(report.auth),
        "text": asdict(report.text),
        "json": asdict(report.json),
        "reasoning": asdict(report.reasoning),
        "vision": asdict(report.vision),
        "role_models": {role.value: model for role, model in report.role_models.items()},
        "models": [asdict(item) for item in report.models],
    }


@router.get("/status")
def get_status(request: Request) -> dict:
    manager = _manager(request)
    status = manager.status()
    last_fallback = status.last_fallback
    return {
        "connections": [
            {
                "connection_id": c.connection_id,
                "model": c.model,
                "circuit_state": c.circuit_state,
                "consecutive_failures": c.consecutive_failures,
                "last_error_code": c.last_error_code,
            }
            for c in status.connections
        ],
        "last_fallback": (
            {
                "primary_provider_id": last_fallback.primary_provider_id,
                "primary_model": last_fallback.primary_model,
                "backup_provider_id": last_fallback.backup_provider_id,
                "backup_model": last_fallback.backup_model,
                "error_code": last_fallback.error_code,
                "occurred_at": last_fallback.occurred_at,
            }
            if last_fallback is not None
            else None
        ),
    }
