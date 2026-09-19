from __future__ import annotations

from typing import Any, TypeVar

import httpx
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


ResultModel = TypeVar("ResultModel", bound=BaseModel)


class OpenAICompatibleClient:
    """Deprecated compatibility client.

    不再自行 POST ``/chat/completions``；改为委托给统一网关适配器
    ``OpenAIChatAdapter``，保留旧的 ``complete_json``/``complete_text`` 接口。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.model = model
        self._adapter = OpenAIChatAdapter(
            base_url=base_url,
            api_key=api_key,
            parameter_style="standard",
            http_client=http_client,
        )
        self._profile = ModelProfile(
            model_id=model,
            roles=frozenset({ModelRole.FAST_TEXT}),
            supported_reasoning_modes=frozenset({ReasoningMode.OFF}),
            supports_json_object=True,
        )

    def _request(
        self,
        messages: list[dict[str, Any]],
        *,
        output_mode: OutputMode,
        response_model: type[BaseModel] | None,
        temperature: float | None,
        execution_context,
        deadline_monotonic: float | None,
    ) -> GenerationRequest:
        return GenerationRequest(
            messages=messages,
            role=ModelRole.FAST_TEXT,
            task_kind=TaskKind.RESUME_PARSE,
            execution_context=execution_context or ExecutionContext.INTERACTIVE,
            output_mode=output_mode,
            reasoning=ReasoningMode.OFF,
            response_model=response_model,
            temperature=temperature,
            deadline_monotonic=deadline_monotonic,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        response_model: type[ResultModel],
        temperature: float | None = None,
        *,
        execution_context=None,
        deadline_monotonic: float | None = None,
    ) -> ResultModel:
        result = await self._adapter.generate(
            self._request(
                messages, output_mode=OutputMode.JSON, response_model=response_model,
                temperature=temperature, execution_context=execution_context,
                deadline_monotonic=deadline_monotonic,
            ),
            model=self.model,
            profile=self._profile,
        )
        return result.parsed  # type: ignore[return-value]

    async def complete_text(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        *,
        execution_context=None,
        deadline_monotonic: float | None = None,
    ) -> str:
        result = await self._adapter.generate(
            self._request(
                messages, output_mode=OutputMode.TEXT, response_model=None,
                temperature=temperature, execution_context=execution_context,
                deadline_monotonic=deadline_monotonic,
            ),
            model=self.model,
            profile=self._profile,
        )
        return result.text
