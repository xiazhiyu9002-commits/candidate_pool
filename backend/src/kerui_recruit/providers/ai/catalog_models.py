"""供应商目录与模型档案 Pydantic 类型。

目录是「能力声明 + 离线种子」，不是能力真相；连接保存前仍须真实探测。
本文件对目录数据执行可执行性约束：仅允许 HTTPS Base URL、已知参数风格、
推荐模型必须存在且声明了对应角色，任何模型不得声明零角色。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, ReasoningMode

# 已知参数兼容风格（首版七个）。
PARAMETER_STYLES = frozenset({
    "standard", "deepseek", "kimi_open", "kimi_code", "qwen", "zhipu", "siliconflow",
})

# 推荐模型必须支持的角色；用于目录校验。
_REASONING_MODES = frozenset(ReasoningMode)
_REASONING_EFFORTS = frozenset({"low", "high", "max"})


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    roles: frozenset[ModelRole] = frozenset()
    supported_reasoning_modes: frozenset[ReasoningMode] = frozenset()
    supported_reasoning_efforts: frozenset[str] = frozenset()
    supports_json_schema: bool = False
    supports_json_object: bool = False
    supports_temperature: bool = True
    deprecated: bool = False

    @model_validator(mode="after")
    def _validate_profile(self) -> "ModelProfile":
        if not self.roles:
            raise ValueError(f"model '{self.model_id}' claims no role")
        for mode in self.supported_reasoning_modes:
            if mode not in _REASONING_MODES:
                raise ValueError(f"model '{self.model_id}' declares unknown reasoning mode {mode!r}")
        for effort in self.supported_reasoning_efforts:
            if effort not in _REASONING_EFFORTS:
                raise ValueError(f"model '{self.model_id}' declares unknown reasoning effort {effort!r}")
        return self


def can_serve_role(profile: ModelProfile, role: ModelRole) -> bool:
    """模型能否服务某角色。

    - 声明了该角色 → 可服务；
    - 思考槽位（reasoning_text）额外放行「可选思考（auto）」与「强制思考（required）」模型，
      使 flash 这类可思考的快速模型也能被手动填入思考槽位并在请求时打开思考；
    - 快速槽位（fast_text）额外放行「强制思考（required）」模型，让使用者能把更强的
      思考模型填进快速模型的位置来换取质量（代价是变慢，这个取舍交给使用者）。
      强制思考模型没有「关思考」开关，放进快速槽位后按它自身的默认行为运行，
      参数映射（``apply_reasoning``）不会给它发不支持的字段。
    """
    if role in profile.roles:
        return True
    if role == ModelRole.REASONING_TEXT:
        return (
            ReasoningMode.AUTO in profile.supported_reasoning_modes
            or ReasoningMode.REQUIRED in profile.supported_reasoning_modes
        )
    if role == ModelRole.FAST_TEXT:
        return ReasoningMode.REQUIRED in profile.supported_reasoning_modes
    return False


class ProviderPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    label: str
    base_url: str
    parameter_style: str
    allowed_contexts: frozenset[ExecutionContext] = frozenset(ExecutionContext)
    models: dict[str, ModelProfile] = {}
    recommended_models: dict[ModelRole, str] = {}
    help_url: str = ""
    key_help_url: str = ""
    subscription_warning: str | None = None
    deprecated: bool = False

    @model_validator(mode="after")
    def _validate_preset(self) -> "ProviderPreset":
        if self.base_url and not self.base_url.startswith("https://"):
            raise ValueError(f"provider '{self.provider_id}' base_url must be HTTPS: {self.base_url!r}")
        if self.parameter_style not in PARAMETER_STYLES:
            raise ValueError(f"provider '{self.provider_id}' has unknown parameter_style {self.parameter_style!r}")
        for role, model_id in self.recommended_models.items():
            model = self.models.get(model_id)
            if model is None:
                raise ValueError(f"provider '{self.provider_id}' recommends missing model {model_id!r}")
            if role not in model.roles:
                raise ValueError(
                    f"provider '{self.provider_id}' recommends model {model_id!r} that does not declare role {role.value}"
                )
        return self


class ProviderCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default_provider_id: str = "deepseek"
    providers: dict[str, ProviderPreset] = {}

    @model_validator(mode="after")
    def _validate_catalog(self) -> "ProviderCatalog":
        for provider_id, preset in self.providers.items():
            if preset.provider_id != provider_id:
                raise ValueError(f"catalog key {provider_id!r} does not match provider_id {preset.provider_id!r}")
        if self.default_provider_id and self.default_provider_id not in self.providers:
            raise ValueError(f"default_provider_id {self.default_provider_id!r} is not a known provider")
        return self
