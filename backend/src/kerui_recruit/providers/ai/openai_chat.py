"""OpenAI Chat Completions 请求与响应适配器。

所有生成式请求最终都收敛到这一个 HTTP 适配器；业务层不得再直接 POST ``/chat/completions``。
响应只读取 ``choices[0].message.content``，丢弃 ``reasoning_content``，并保留上游 request id、连接/模型身份与耗时。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from kerui_recruit.providers.ai.catalog_models import ModelProfile
from kerui_recruit.providers.ai.contracts import GenerationRequest, GenerationResult
from kerui_recruit.providers.ai.parameter_mapping import apply_json_format, apply_reasoning, apply_temperature
from kerui_recruit.providers.errors import FailureCategory, ProviderError, map_http_error

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_CONNECT_SECONDS = 10.0


def _read_error_body(response: httpx.Response) -> str | None:
    """读取上游错误正文仅用于内存分类，不返回、不记录。"""
    try:
        return response.text
    except Exception:
        return None


def _strip_json_fence(text: str) -> str:
    """剥离模型可能包裹在 ```json ... ``` 围栏里的结构化输出。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _extract_json_object(text: str) -> str | None:
    """从自由文本中截出第一个括号平衡的 JSON 对象。

    模型常在前言/结语里夹带解释（尤其上游不支持 ``response_format`` 时）。
    这里只做花括号配对并跳过字符串字面量，不做语义校验；找不到完整对象返回 None。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头（秒）；无法解析返回 None。"""
    if value is None:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


class OpenAIChatAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        parameter_style: str,
        http_client: httpx.AsyncClient,
        connection_id: str = "",
        provider_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.parameter_style = parameter_style
        self.http_client = http_client
        self.connection_id = connection_id
        self.provider_id = provider_id

    async def generate(
        self,
        request: GenerationRequest,
        *,
        model: str,
        profile: ModelProfile,
    ) -> GenerationResult:
        started = time.monotonic()
        body: dict[str, Any] = {"model": model, "messages": request.messages}
        apply_temperature(body, temperature=request.temperature, profile=profile)
        apply_reasoning(body, style=self.parameter_style, mode=request.reasoning, effort=request.reasoning_effort, profile=profile)
        apply_json_format(body, profile=profile, response_model=request.response_model)

        timeout = self._timeout(request.deadline_monotonic)
        try:
            response = await self.http_client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=timeout,
            )
        except httpx.RequestError as error:
            raise ProviderError(
                code="E_API_NETWORK",
                retryable=True,
                user_message="无法连接 API 服务",
                category=FailureCategory.NETWORK,
                switchable=True,
                connection_id=self.connection_id,
                provider_id=self.provider_id,
                model=model,
            ) from error

        request_id = response.headers.get("x-request-id")
        if response.status_code >= 400:
            mapped = map_http_error(
                response.status_code,
                request_id=request_id,
                error_body=_read_error_body(response),
                retry_after_seconds=_parse_retry_after(response.headers.get("retry-after")),
            )
            raise ProviderError(
                code=mapped.code,
                retryable=mapped.retryable,
                user_message=mapped.user_message,
                request_id=mapped.request_id,
                category=mapped.category,
                switchable=mapped.switchable,
                connection_id=self.connection_id,
                provider_id=self.provider_id,
                model=model,
                retry_after_seconds=mapped.retry_after_seconds,
            )

        try:
            payload: dict[str, Any] = response.json()
            content = payload["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderError(
                code="E_API_SCHEMA",
                retryable=True,
                user_message="API 返回内容不符合结构要求",
                request_id=request_id,
                category=FailureCategory.SCHEMA,
                switchable=True,
                connection_id=self.connection_id,
                provider_id=self.provider_id,
                model=model,
            ) from error

        if content is None or not str(content).strip():
            raise ProviderError(
                code="E_API_EMPTY_CONTENT",
                retryable=True,
                user_message="API 返回内容为空",
                request_id=request_id,
                category=FailureCategory.SCHEMA,
                switchable=True,
                connection_id=self.connection_id,
                provider_id=self.provider_id,
                model=model,
            )

        text = str(content).strip()
        parsed: BaseModel | None = None
        if request.response_model is not None:
            candidate = _strip_json_fence(text)
            try:
                parsed = request.response_model.model_validate_json(candidate)
            except ValidationError as error:
                # 容错提取：上游不支持 response_format 时模型常带前言/结语，
                # 先按花括号平衡截出 JSON 对象再试一次，避免直接退化成非结构化输出。
                extracted = _extract_json_object(candidate)
                recovered: BaseModel | None = None
                if extracted is not None and extracted != candidate:
                    try:
                        recovered = request.response_model.model_validate_json(extracted)
                    except ValidationError:
                        recovered = None
                if recovered is None:
                    raise ProviderError(
                        code="E_API_SCHEMA",
                        retryable=True,
                        user_message="API 返回内容不符合结构要求",
                        request_id=request_id,
                        category=FailureCategory.SCHEMA,
                        switchable=True,
                        connection_id=self.connection_id,
                        provider_id=self.provider_id,
                        model=model,
                    ) from error
                parsed = recovered
                logger.warning(
                    "上游返回内容不是纯 JSON，已按花括号平衡抽取后解析成功：connection=%s model=%s",
                    self.connection_id, model,
                )

        return GenerationResult(
            text=text,
            parsed=parsed,
            connection_id=self.connection_id,
            provider_id=self.provider_id,
            model=model,
        )

    def _timeout(self, deadline_monotonic: float | None) -> httpx.Timeout:
        read_timeout = _DEFAULT_TIMEOUT_SECONDS
        connect_timeout = _DEFAULT_CONNECT_SECONDS
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    code="E_AI_DEADLINE",
                    retryable=False,
                    user_message="请求预算已耗尽",
                    category=FailureCategory.DEADLINE,
                    switchable=False,
                    connection_id=self.connection_id,
                    provider_id=self.provider_id,
                )
            read_timeout = min(read_timeout, remaining)
            connect_timeout = min(connect_timeout, remaining)
        return httpx.Timeout(read_timeout, connect=connect_timeout)
