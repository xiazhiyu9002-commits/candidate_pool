from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationRequest,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.openai_chat import OpenAIChatAdapter
from kerui_recruit.providers.errors import FailureCategory, ProviderError


def profile(**kwargs) -> ModelProfile:
    defaults = dict(
        model_id="model-one",
        roles=frozenset({ModelRole.FAST_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF}),
        supported_reasoning_efforts=frozenset(),
        supports_json_object=True,
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def text_request() -> GenerationRequest:
    return GenerationRequest(
        messages=[{"role": "user", "content": "hi"}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.QUERY_REWRITE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.OFF,
    )


class JsonPayload(BaseModel):
    ok: bool


def json_request() -> GenerationRequest:
    return GenerationRequest(
        messages=[{"role": "user", "content": "return ok"}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.QUERY_REWRITE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.JSON,
        reasoning=ReasoningMode.OFF,
        response_model=JsonPayload,
    )


def adapter_returning(payload: dict, *, status: int = 200) -> OpenAIChatAdapter:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIChatAdapter(
        base_url="https://example.com",
        api_key="k",
        parameter_style="standard",
        http_client=client,
        connection_id="c1",
        provider_id="deepseek",
    )


@pytest.mark.asyncio
async def test_adapter_uses_final_content_and_discards_reasoning_content():
    adapter = adapter_returning({
        "choices": [{"message": {"reasoning_content": "private chain", "content": "final answer"}}],
    })
    result = await adapter.generate(text_request(), model="model-one", profile=profile())
    assert result.text == "final answer"
    assert "private chain" not in repr(result)


@pytest.mark.asyncio
async def test_empty_final_content_is_switchable_schema_error():
    adapter = adapter_returning({
        "choices": [{"message": {"reasoning_content": "only thought", "content": ""}}],
    })
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(text_request(), model="model-one", profile=profile())
    assert caught.value.code == "E_API_EMPTY_CONTENT"
    assert caught.value.switchable is True


@pytest.mark.asyncio
async def test_json_parsed_into_model():
    adapter = adapter_returning({"choices": [{"message": {"content": '{"ok": true}'}}]})
    result = await adapter.generate(json_request(), model="model-one", profile=profile())
    assert result.parsed == JsonPayload(ok=True)


@pytest.mark.asyncio
async def test_json_fenced_output_is_stripped_before_validation():
    adapter = adapter_returning({"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]})
    result = await adapter.generate(json_request(), model="model-one", profile=profile())
    assert result.parsed == JsonPayload(ok=True)


@pytest.mark.asyncio
async def test_json_with_preamble_is_recovered_by_brace_extraction():
    """上游不支持 response_format 时会带前言/结语，应按花括号平衡截出 JSON 再解析。"""
    adapter = adapter_returning({
        "choices": [{"message": {"content": '好的，结果如下：{"ok": true}\n以上。'}}],
    })
    result = await adapter.generate(json_request(), model="model-one", profile=profile())
    assert result.parsed == JsonPayload(ok=True)


@pytest.mark.asyncio
async def test_json_with_nested_braces_in_string_is_recovered():
    """抽取必须理解字符串与转义，字符串里的花括号不能干扰括号计数。"""
    adapter = adapter_returning({
        "choices": [{"message": {"content": '前言 {"ok": true, "note": "a } b"} 结语'}}],
    })
    result = await adapter.generate(json_request(), model="model-one", profile=profile())
    assert result.parsed == JsonPayload(ok=True)


@pytest.mark.asyncio
async def test_unrecoverable_braces_still_raise_schema_error():
    """花括号不平衡时不得误判成功，仍按结构错误上报。"""
    adapter = adapter_returning({"choices": [{"message": {"content": "前言 {不是 JSON"}}]})
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(json_request(), model="model-one", profile=profile())
    assert caught.value.code == "E_API_SCHEMA"


@pytest.mark.asyncio
async def test_json_validation_failure_is_switchable_schema_error():
    adapter = adapter_returning({"choices": [{"message": {"content": "not json"}}]})
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(json_request(), model="model-one", profile=profile())
    assert caught.value.code == "E_API_SCHEMA"
    assert caught.value.category is FailureCategory.SCHEMA
    assert caught.value.switchable is True


@pytest.mark.asyncio
async def test_http_error_maps_switchable_category():
    adapter = adapter_returning({"error": "busy"}, status=503)
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(text_request(), model="model-one", profile=profile())
    assert caught.value.code == "E_API_BUSY"
    assert caught.value.category is FailureCategory.SERVER
    assert caught.value.switchable is True
    assert caught.value.provider_id == "deepseek"


@pytest.mark.asyncio
async def test_expired_deadline_raises_non_switchable():
    import time

    adapter = adapter_returning({"choices": [{"message": {"content": "late"}}]})
    request = GenerationRequest(
        messages=[{"role": "user", "content": "hi"}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.QUERY_REWRITE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.OFF,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(request, model="model-one", profile=profile())
    assert caught.value.code == "E_AI_DEADLINE"
    assert caught.value.switchable is False
