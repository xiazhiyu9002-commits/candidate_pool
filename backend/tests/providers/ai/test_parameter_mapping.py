from __future__ import annotations

import pytest

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.contracts import ModelRole, ReasoningMode
from kerui_recruit.providers.ai.parameter_mapping import apply_json_format, apply_reasoning, apply_temperature


def profile(**kwargs) -> ModelProfile:
    defaults = dict(
        model_id="test",
        roles=frozenset({ModelRole.FAST_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF, ReasoningMode.REQUIRED}),
        supported_reasoning_efforts=frozenset({"low", "high"}),
        supports_json_schema=True,
        supports_json_object=True,
        supports_temperature=True,
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


@pytest.mark.parametrize(
    ("style", "mode", "expected"),
    [
        ("deepseek", ReasoningMode.REQUIRED, {"thinking": {"type": "enabled"}}),
        ("deepseek", ReasoningMode.OFF, {"thinking": {"type": "disabled"}}),
        ("kimi_open", ReasoningMode.REQUIRED, {"thinking": {"type": "enabled"}}),
        ("qwen", ReasoningMode.REQUIRED, {"enable_thinking": True}),
        ("zhipu", ReasoningMode.OFF, {"thinking": {"type": "disabled"}}),
        ("siliconflow", ReasoningMode.OFF, {"enable_thinking": False}),
        ("standard", ReasoningMode.REQUIRED, {}),
    ],
)
def test_reasoning_mapping(style, mode, expected):
    body = {}
    apply_reasoning(body, style=style, mode=mode, effort=None, profile=profile())
    for key, value in expected.items():
        assert body[key] == value


def test_always_thinking_model_omits_thinking_and_sends_effort():
    # 强制思考模型（无 OFF）不发送 thinking 开关，只发送 reasoning_effort。
    body = {}
    apply_reasoning(
        body,
        style="kimi_open",
        mode=ReasoningMode.REQUIRED,
        effort="high",
        profile=profile(supported_reasoning_modes=frozenset({ReasoningMode.AUTO, ReasoningMode.REQUIRED})),
    )
    assert "thinking" not in body
    assert body["reasoning_effort"] == "high"


def test_always_thinking_siliconflow_sends_budget_not_toggle():
    body = {}
    apply_reasoning(
        body,
        style="siliconflow",
        mode=ReasoningMode.REQUIRED,
        effort="max",
        profile=profile(
            supported_reasoning_modes=frozenset({ReasoningMode.AUTO, ReasoningMode.REQUIRED}),
            supported_reasoning_efforts=frozenset({"low", "high", "max"}),
        ),
    )
    assert "enable_thinking" not in body
    assert body["thinking_budget"] == "max"


def test_togglable_model_sends_thinking_toggle():
    # 可切换思考模型（含 OFF）保留 thinking 开关。
    body = {}
    apply_reasoning(
        body,
        style="kimi_open",
        mode=ReasoningMode.REQUIRED,
        effort=None,
        profile=profile(supported_reasoning_modes=frozenset({ReasoningMode.OFF, ReasoningMode.AUTO})),
    )
    assert body["thinking"] == {"type": "enabled"}


def test_off_only_model_omits_thinking_toggle():
    # off-only 模型（只有 OFF）本身不思考，也不接受思考开关字段，不应发送 thinking。
    body = {}
    apply_reasoning(
        body,
        style="zhipu",
        mode=ReasoningMode.OFF,
        effort=None,
        profile=profile(supported_reasoning_modes=frozenset({ReasoningMode.OFF})),
    )
    assert "thinking" not in body


def test_reasoning_effort_only_when_supported():
    body = {}
    apply_reasoning(body, style="deepseek", mode=ReasoningMode.REQUIRED, effort="high", profile=profile())
    assert body["reasoning_effort"] == "high"

    body = {}
    apply_reasoning(
        body, style="deepseek", mode=ReasoningMode.REQUIRED, effort="max",
        profile=profile(supported_reasoning_efforts=frozenset({"low", "high"})),
    )
    assert "reasoning_effort" not in body


def test_temperature_omitted_when_unsupported():
    body = {}
    apply_temperature(body, temperature=0.5, profile=profile())
    assert body["temperature"] == 0.5

    body = {}
    apply_temperature(body, temperature=0.5, profile=profile(supports_temperature=False))
    assert "temperature" not in body


# 订阅平台与同厂商按量计费平台的**参数口径**对应关系（2026-09-22 勘察）。
#
# 用户口径是「除了 api 与 url 不同，请求体基本一致」——这决定了「只测按量计费平台就能
# 覆盖订阅平台」这条结论能不能站住。两个订阅平台直接复用按量计费的 parameter_style
# （由 catalog 断言守着），只有 Kimi 的订阅端点是独立 style 名，所以这里必须逐字段证明
# 两者产出的请求体**完全相同**：style 名不同不等于行为不同，但也不能靠"看起来一样"下结论。
_SUBSCRIPTION_STYLE_PEERS = (("kimi_code", "kimi_open"),)


@pytest.mark.parametrize(("subscription_style", "api_style"), _SUBSCRIPTION_STYLE_PEERS)
@pytest.mark.parametrize(
    "model_profile",
    [
        profile(),  # 可切换思考 + 支持 effort + 支持 temperature
        profile(supported_reasoning_modes=frozenset({ReasoningMode.OFF})),  # off-only
        profile(supported_reasoning_modes=frozenset({ReasoningMode.AUTO, ReasoningMode.REQUIRED})),  # 强制思考
        profile(supports_json_schema=False, supports_json_object=True),
        profile(supports_json_schema=False, supports_json_object=False),
        profile(supports_temperature=False),
    ],
)
def test_subscription_style_writes_the_same_body_as_api_platform(
    subscription_style, api_style, model_profile,
):
    """订阅 style 与按量计费 style 在**同一个模型档案**下必须写出逐字段相同的请求体。"""
    from pydantic import BaseModel

    class Payload(BaseModel):
        name: str

    for mode in ReasoningMode:
        for effort in (None, "low", "max"):
            bodies = []
            for style in (subscription_style, api_style):
                body: dict = {}
                apply_reasoning(body, style=style, mode=mode, effort=effort, profile=model_profile)
                apply_json_format(body, profile=model_profile, response_model=Payload)
                apply_temperature(body, temperature=0.3, profile=model_profile)
                bodies.append(body)
            assert bodies[0] == bodies[1], (
                f"{subscription_style} 与 {api_style} 在 mode={mode} effort={effort} "
                f"profile={model_profile.model_id} 下请求体不一致：{bodies}"
            )


def test_subscription_providers_reuse_their_api_platform_parameter_style(tmp_path):
    """两个订阅平台直接复用按量计费的 parameter_style；改成分叉的 style 名会让上面的
    请求体等价性失去结构保证，因此在这里钉住。"""
    from kerui_recruit.providers.ai.catalog import CatalogService

    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert providers["qwen_code"].parameter_style == providers["qwen"].parameter_style
    assert providers["zhipu_code"].parameter_style == providers["zhipu"].parameter_style


def test_always_thinking_preset_gets_no_reasoning_toggle(tmp_path):
    """真机缺陷的请求体级守卫：「始终思考」的智谱预设放进快速槽位时**不得**发关思考字段。

    目录档案把「始终思考」模型误声明成支持 OFF 时，`apply_reasoning` 会写出
    `thinking: {"type": "disabled"}`，对端直接 400（表现为 `E_API_FORMAT`）。智谱
    2026-09-22 的原文是「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。
    快速槽位的调用正是 `reasoning=OFF`，所以这里按快速槽位的真实形态断言请求体为空。
    """
    from kerui_recruit.providers.ai.catalog import CatalogService

    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    targets = (
        ("zhipu", "glm-5.3-flash"),
        ("zhipu", "glm-5.3-flashx"),
        ("zhipu_code", "glm-5.3-flash"),
    )
    for provider_id, model_id in targets:
        preset = providers[provider_id]
        body: dict = {}
        apply_reasoning(
            body,
            style=preset.parameter_style,
            mode=ReasoningMode.OFF,
            effort=None,
            profile=preset.models[model_id],
        )
        assert body == {}, (provider_id, model_id)


def test_no_effort_when_thinking_is_explicitly_disabled(tmp_path):
    """关思考时**不得**发强度：阿里 DashScope 直接回 400。

    真机原文（2026-09-22）：
    `'reasoning_effort' must be 'none' when 'enable_thinking' is false`。
    这个 400 被 `QueryParser` 吞成 `degraded=provider_error`，表现成「AI 智能解析永远
    回退规则链路」——排查时只能看到「解析没生效」，看不到真实原因。所以钉在请求体层面。
    """
    from kerui_recruit.providers.ai.catalog import CatalogService

    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    preset = providers["qwen"]
    for effort in ("low", "high"):
        body: dict = {}
        apply_reasoning(
            body, style=preset.parameter_style, mode=ReasoningMode.OFF,
            effort=effort, profile=preset.models["qwen3.8-flash"],
        )
        assert body == {"enable_thinking": False}, (effort, body)


def test_effort_is_sent_when_thinking_is_on_for_a_togglable_model():
    """思考开着时可以发强度（实测阿里 开思考+low 正常返回，只是慢）。"""
    body: dict = {}
    apply_reasoning(
        body, style="qwen", mode=ReasoningMode.AUTO, effort="low",
        profile=profile(supported_reasoning_modes=frozenset({ReasoningMode.OFF, ReasoningMode.AUTO})),
    )
    assert body == {"enable_thinking": True, "reasoning_effort": "low"}


def test_json_format_omitted_when_unsupported():
    from pydantic import BaseModel

    class Foo(BaseModel):
        a: str

    body = {}
    apply_json_format(body, profile=profile(), response_model=Foo)
    assert body["response_format"]["type"] == "json_schema"

    body = {}
    apply_json_format(body, profile=profile(supports_json_schema=False), response_model=Foo)
    assert body["response_format"]["type"] == "json_object"

    body = {}
    apply_json_format(
        body,
        profile=profile(supports_json_schema=False, supports_json_object=False),
        response_model=Foo,
    )
    assert "response_format" not in body
