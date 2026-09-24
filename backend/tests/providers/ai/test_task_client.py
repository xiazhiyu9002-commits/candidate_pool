from __future__ import annotations

import pytest
from pydantic import BaseModel

from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationResult,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.task_client import TaskGenerationClient
from kerui_recruit.providers.generation_tasks import AiResumeParser
from kerui_recruit.resumes.structured import ParsedResume


class _EmptyModel(BaseModel):
    pass


class CapturingGenerationClient:
    def __init__(self, *, parsed=None, text: str = "") -> None:
        self.requests = []
        self.parsed = parsed
        self.text = text

    async def generate(self, request):
        self.requests.append(request)
        return GenerationResult(
            text=self.text, parsed=self.parsed, connection_id="", provider_id="", model="",
        )


@pytest.mark.asyncio
async def test_resume_parser_requests_fast_nonthinking_background_json():
    gateway = CapturingGenerationClient(parsed=ParsedResume(name="张三"))
    parser = AiResumeParser(TaskGenerationClient(
        gateway,
        task_kind=TaskKind.RESUME_PARSE,
        role=ModelRole.FAST_TEXT,
        execution_context=ExecutionContext.BACKGROUND,
    ))
    result = await parser.parse_resume("张三简历")
    assert result.name == "张三"
    request = gateway.requests[0]
    assert (request.role, request.reasoning, request.output_mode) == (
        ModelRole.FAST_TEXT, ReasoningMode.OFF, OutputMode.JSON,
    )
    assert request.execution_context is ExecutionContext.BACKGROUND
    assert request.task_kind is TaskKind.RESUME_PARSE


@pytest.mark.asyncio
async def test_complete_text_returns_text():
    gateway = CapturingGenerationClient(text="ok")
    client = TaskGenerationClient(
        gateway,
        task_kind=TaskKind.QUERY_REWRITE,
        role=ModelRole.FAST_TEXT,
        execution_context=ExecutionContext.INTERACTIVE,
    )
    text = await client.complete_text([{"role": "user", "content": "a"}])
    assert text == "ok"
    assert gateway.requests[0].output_mode is OutputMode.TEXT


@pytest.mark.asyncio
async def test_cache_identity_changes_with_task_and_role():
    gateway = CapturingGenerationClient()
    a = TaskGenerationClient(gateway, task_kind=TaskKind.RESUME_PARSE, role=ModelRole.FAST_TEXT, execution_context=ExecutionContext.BACKGROUND)
    b = TaskGenerationClient(gateway, task_kind=TaskKind.BD_PLAN, role=ModelRole.REASONING_TEXT, execution_context=ExecutionContext.INTERACTIVE)
    assert a.cache_identity != b.cache_identity
    assert "resume_parse" in a.cache_identity
    assert "bd_plan" in b.cache_identity


@pytest.mark.asyncio
async def test_complete_text_forwards_max_tokens_and_defaults_to_none():
    """输出上界必须一路传到请求里；没给时保持 None —— 结构化调用一个字段都不该发。"""
    gateway = CapturingGenerationClient(text="ok")
    client = TaskGenerationClient(gateway, task_kind=TaskKind.CANDIDATE_PROFILE,
                                  role=ModelRole.FAST_TEXT,
                                  execution_context=ExecutionContext.INTERACTIVE)

    await client.complete_text([{"role": "user", "content": "a"}], max_tokens=1024)
    await client.complete_text([{"role": "user", "content": "a"}])

    assert gateway.requests[0].max_tokens == 1024
    assert gateway.requests[1].max_tokens is None


@pytest.mark.asyncio
async def test_configured_reasoning_effort_reaches_the_request():
    """任务级思考强度必须落到 `GenerationRequest.reasoning_effort`。

    没接上就等于没设置：`parameter_mapping.apply_reasoning` 只看这个字段，
    请求体里不会出现 `reasoning_effort`，实测该模型会按自身默认行为思考
    （智谱 glm-5.3-flashx 不指定档位 11.2 秒，`low` 2.0 秒）。
    """
    gateway = CapturingGenerationClient(parsed=_EmptyModel(), text="ok")
    client = TaskGenerationClient(
        gateway,
        task_kind=TaskKind.QUERY_PARSE,
        role=ModelRole.FAST_TEXT,
        execution_context=ExecutionContext.INTERACTIVE,
        reasoning_effort="low",
        prefer_off=True,
    )
    await client.complete_text([{"role": "user", "content": "a"}])
    await client.complete_json([{"role": "user", "content": "a"}], _EmptyModel)
    assert [item.reasoning_effort for item in gateway.requests] == ["low", "low"]
    # 「宁可关思考」也要一路传到请求上：它决定槽位配的强度参不参与。
    assert [item.prefer_off for item in gateway.requests] == [True, True]


@pytest.mark.asyncio
async def test_no_configured_reasoning_effort_leaves_it_none():
    """没配档位时一个字段都不该发：能否接受某个档位由模型档案决定，不由我们替它猜。"""
    gateway = CapturingGenerationClient()
    client = TaskGenerationClient(
        gateway,
        task_kind=TaskKind.QUERY_REWRITE,
        role=ModelRole.FAST_TEXT,
        execution_context=ExecutionContext.INTERACTIVE,
    )
    await client.complete_text([{"role": "user", "content": "a"}])
    assert gateway.requests[0].reasoning_effort is None
