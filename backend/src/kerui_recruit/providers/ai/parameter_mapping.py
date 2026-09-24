"""供应商参数映射：把统一请求转换为各供应商 OpenAI Chat Completions 兼容字段。

仅发送模型档案声明支持的字段；未知自定义连接默认只发送标准字段。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.contracts import ReasoningMode

logger = logging.getLogger(__name__)


def apply_reasoning(
    body: dict[str, Any],
    *,
    style: str,
    mode: ReasoningMode,
    effort: str | None,
    profile: ModelProfile,
) -> None:
    """按模型档案写入思考开关字段；仅在档案支持时写入思考强度。

    - 可切换思考模型（同时支持 OFF 与 AUTO/REQUIRED）：发送 thinking/enable_thinking 开关。
    - 强制思考模型（无 OFF，如 kimi-k3、deepseek-v4-pro）：不发送思考开关（默认即思考），
      仅在命中 supported_reasoning_efforts 时发送 reasoning_effort / thinking_budget。
    - off-only 模型（只有 OFF）：不发送思考开关。

    **本次已经关掉思考时不发强度**：阿里 DashScope 对 `enable_thinking=false` 与
    `reasoning_effort` 的组合直接回 400 ——
    `'reasoning_effort' must be 'none' when 'enable_thinking' is false`。
    实测（2026-09-22）这个 400 被 `QueryParser` 吞成 `degraded=provider_error`，
    表现成「AI 智能解析永远回退规则链路」，排查时看不到真实原因。
    所以这里是第二层守卫：调用方（`router`）已经会算好，但组合一旦漏下来必须是安静的正确值。
    """
    # 仅「可切换思考」模型（同时支持非思考与思考）才发送 thinking/enable_thinking 开关；
    # off-only 模型（只有 OFF）本身不思考，也不接受思考开关字段，不应发送。
    togglable = (
        ReasoningMode.OFF in profile.supported_reasoning_modes
        and (
            ReasoningMode.AUTO in profile.supported_reasoning_modes
            or ReasoningMode.REQUIRED in profile.supported_reasoning_modes
        )
    )
    thinking_disabled = togglable and mode == ReasoningMode.OFF

    if togglable:
        if mode == ReasoningMode.OFF:
            if style == "deepseek":
                body["thinking"] = {"type": "disabled"}
            elif style in ("kimi_open", "kimi_code"):
                body["thinking"] = {"type": "disabled"}
            elif style == "qwen":
                body["enable_thinking"] = False
            elif style == "zhipu":
                body["thinking"] = {"type": "disabled"}
            elif style == "siliconflow":
                body["enable_thinking"] = False
            # standard / unknown：不发送思考字段。
        elif mode in (ReasoningMode.REQUIRED, ReasoningMode.AUTO):
            if style == "deepseek":
                body["thinking"] = {"type": "enabled"}
            elif style in ("kimi_open", "kimi_code"):
                body["thinking"] = {"type": "enabled"}
            elif style == "qwen":
                body["enable_thinking"] = True
            elif style == "zhipu":
                body["thinking"] = {"type": "enabled"}
            elif style == "siliconflow":
                body["enable_thinking"] = True
            # standard / unknown：不发送思考字段。

    if effort is not None and effort in profile.supported_reasoning_efforts and not thinking_disabled:
        if style == "siliconflow":
            body["thinking_budget"] = effort
        elif style in ("deepseek", "kimi_open", "kimi_code", "qwen", "zhipu"):
            body["reasoning_effort"] = effort


def apply_json_format(
    body: dict[str, Any],
    *,
    profile: ModelProfile,
    response_model: type[BaseModel] | None,
) -> None:
    """按模型能力选择 JSON Schema / JSON Object，均不支持则不发送 response_format。"""
    if response_model is None:
        return
    if profile.supports_json_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
            },
        }
    elif profile.supports_json_object:
        body["response_format"] = {"type": "json_object"}
    else:
        # 不允许静默：两种 JSON 能力都不支持时必须留痕，否则上游会按普通文本回答，
        # 结构化校验失败后又会被上层当成「模型不听话」而掩盖真实原因。
        logger.warning(
            "模型 %s 未声明 JSON 能力，本次未发送 response_format；结构化输出只能依赖提示词与容错解析",
            profile.model_id,
        )


def apply_temperature(
    body: dict[str, Any],
    *,
    temperature: float | None,
    profile: ModelProfile,
) -> None:
    """仅当模型声明支持 temperature 且调用方显式传值时发送该字段。"""
    if temperature is not None and profile.supports_temperature:
        body["temperature"] = temperature
