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
