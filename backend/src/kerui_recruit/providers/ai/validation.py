"""统一的生成式 AI 连接校验。

保存（PUT /api/ai/config）与探测（POST /api/ai/probe）在任何外部请求发生前都必须
先经过这里，保证：

- 预设供应商不能覆盖 Base URL 或兼容风格；
- custom_openai 必须提供 HTTPS Base URL（仅显式测试开关允许 127.0.0.1 HTTP）；
- 兼容风格必须落在后端已知枚举内。
"""
from __future__ import annotations

from kerui_recruit.providers.ai.catalog_models import PARAMETER_STYLES, ProviderCatalog, can_serve_role
from kerui_recruit.providers.ai.config_models import AiConnection

# 内置预设供应商：结构由内置目录决定，前端不得覆盖 Base URL / 兼容风格。
_PRESET_PROVIDERS = frozenset({
    "deepseek", "kimi_open", "kimi_code", "qwen", "zhipu", "siliconflow",
})


def validate_connection(
    connection: AiConnection,
    catalog: ProviderCatalog,
    *,
    allow_insecure_loopback: bool = False,
) -> None:
    """校验单个连接；违规抛出 ``ValueError``（由 API 层转成 422）。"""
    preset = catalog.providers.get(connection.provider_id)
    if preset is None:
        raise ValueError(f"unknown provider_id: {connection.provider_id}")

    if connection.parameter_style is not None and connection.parameter_style not in PARAMETER_STYLES:
        raise ValueError(f"unknown parameter_style: {connection.parameter_style}")

    # 已知目录模型被分配到其未声明的角色 → 拒绝（目录外模型不在此列，靠真实探测）。
    # 例外：思考槽位放行「可选思考/强制思考」模型（can_serve_role 判定）。
    for role, model_id in connection.models.items():
        profile = preset.models.get(model_id)
        if profile is not None and not can_serve_role(profile, role):
            raise ValueError(f"模型 {model_id} 不支持角色 {role.value}")

    # 思考强度只能取该角色所用模型档案声明过的档位。**目录外模型一律拒绝**：
    # 自定义连接没有档位声明，界面也就没有可选项；这里放行的话，
    # `apply_reasoning` 会因为档位不在 `supported_reasoning_efforts` 里而静默不发字段，
    # 使用者以为设了强度、实际请求体里什么都没有。
    for role, effort in connection.reasoning_efforts.items():
        model_id = connection.models.get(role)
        if model_id is None:
            raise ValueError(f"角色 {role.value} 未分配模型，不能指定思考强度")
        profile = preset.models.get(model_id)
        if profile is None:
            raise ValueError(f"模型 {model_id} 未声明思考强度档位，不能指定思考强度 {effort}")
        if effort not in profile.supported_reasoning_efforts:
            supported = "、".join(sorted(profile.supported_reasoning_efforts)) or "无"
            raise ValueError(f"模型 {model_id} 不支持思考强度 {effort}（可用：{supported}）")

    if connection.provider_id == "custom_openai":
        base = connection.base_url_override or ""
        if not base.startswith("https://") and not (
            allow_insecure_loopback and base.startswith("http://127.0.0.1")
        ):
            raise ValueError("custom_openai 必须提供 HTTPS Base URL")
        return

    # 预设供应商：不得覆盖 Base URL 或兼容风格，只能使用内置目录值。
    if connection.base_url_override:
        raise ValueError(f"预设供应商 {connection.provider_id} 不允许覆盖 Base URL")
    if connection.parameter_style is not None:
        raise ValueError(f"预设供应商 {connection.provider_id} 不允许覆盖兼容风格")
