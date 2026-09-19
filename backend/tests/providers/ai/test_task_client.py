from __future__ import annotations

import pytest

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
