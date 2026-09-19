from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureCategory(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    MODEL = "model"
    SCHEMA = "schema"
    INPUT = "input"
    POLICY = "policy"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    UNKNOWN = "unknown"


@dataclass(eq=False)
class ProviderError(RuntimeError):
    code: str
    retryable: bool
    user_message: str
    request_id: str | None = None
    category: FailureCategory = FailureCategory.UNKNOWN
    switchable: bool = False
    connection_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    details: tuple | None = None
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        suffix = f" ({self.request_id})" if self.request_id else ""
        return f"{self.code}: {self.user_message}{suffix}"


# (code, retryable, message, category, switchable)
_STATUS_ERRORS: dict[int, tuple[str, bool, str, FailureCategory, bool]] = {
    400: ("E_API_FORMAT", False, "请求格式不正确", FailureCategory.INPUT, False),
    401: ("E_API_AUTH", False, "API 密钥无效或无权限", FailureCategory.AUTH, True),
    402: ("E_API_BALANCE", False, "API 账户余额不足", FailureCategory.QUOTA, True),
    403: ("E_API_AUTH", False, "API 密钥无效或无权限", FailureCategory.AUTH, True),
    408: ("E_API_TIMEOUT", True, "API 请求超时", FailureCategory.TIMEOUT, True),
    422: ("E_API_PARAMETERS", False, "API 请求参数不正确", FailureCategory.INPUT, False),
    429: ("E_API_RATE_LIMIT", True, "API 调用频率达到上限", FailureCategory.RATE_LIMIT, True),
    500: ("E_API_UPSTREAM", True, "API 服务暂时异常", FailureCategory.SERVER, True),
    502: ("E_API_UPSTREAM", True, "API 服务暂时异常", FailureCategory.SERVER, True),
    503: ("E_API_BUSY", True, "API 服务繁忙", FailureCategory.SERVER, True),
    504: ("E_API_UPSTREAM", True, "API 服务超时", FailureCategory.TIMEOUT, True),
}

_MODEL_HINTS = (
    "not found", "deprecated", "retired", "no permission", "unknown model",
    "no such model", "model not available", "model not supported", "capability not supported",
)
_POLICY_HINTS = ("content policy", "safety", "moderation", "unsafe", "not allowed")
_INPUT_HINTS = (
    "too long", "too many tokens", "context length", "max context", "image format",
    "invalid image", "unsupported image", "image size", "exceed",
)


def _classify_by_body(body: str) -> FailureCategory | None:
    """仅用于内存分类；正文不被返回或记录。"""
    lowered = body.lower()
    for hint in _MODEL_HINTS:
        if hint in lowered:
            return FailureCategory.MODEL
    for hint in _POLICY_HINTS:
        if hint in lowered:
            return FailureCategory.POLICY
    for hint in _INPUT_HINTS:
        if hint in lowered:
            return FailureCategory.INPUT
    return None


def map_http_error(status: int, *, request_id: str | None = None, error_body: str | None = None, retry_after_seconds: float | None = None) -> ProviderError:
    if error_body:
        body_category = _classify_by_body(error_body)
        if body_category == FailureCategory.MODEL:
            return ProviderError(
                code="E_API_MODEL",
                retryable=False,
                user_message="模型不存在、已下架或无权限",
                request_id=request_id,
                category=FailureCategory.MODEL,
                switchable=True,
            )
        if body_category == FailureCategory.POLICY:
            return ProviderError(
                code="E_API_POLICY",
                retryable=False,
                user_message="内容被安全策略拒绝",
                request_id=request_id,
                category=FailureCategory.POLICY,
                switchable=False,
            )
        if body_category == FailureCategory.INPUT:
            return ProviderError(
                code="E_API_INPUT",
                retryable=False,
                user_message="请求超出限制或格式不支持",
                request_id=request_id,
                category=FailureCategory.INPUT,
                switchable=False,
            )
    code, retryable, message, category, switchable = _STATUS_ERRORS.get(
        status,
        (
            "E_API_HTTP",
            status >= 500,
            f"API 返回 HTTP {status}",
            FailureCategory.SERVER if status >= 500 else FailureCategory.UNKNOWN,
            status >= 500,
        ),
    )
    return ProviderError(
        code=code,
        retryable=retryable,
        user_message=message,
        request_id=request_id,
        category=category,
        switchable=switchable,
        retry_after_seconds=retry_after_seconds,
    )
