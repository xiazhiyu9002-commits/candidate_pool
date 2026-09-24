from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ModelRole(StrEnum):
    FAST_TEXT = "fast_text"
    REASONING_TEXT = "reasoning_text"
    VISION = "vision"


class ExecutionContext(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    BATCH = "batch"


class ReasoningMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


# 思考强度档位。**取值必须与模型档案的 `supported_reasoning_efforts` 同一套**：
# `parameter_mapping.apply_reasoning` 只在 `effort in profile.supported_reasoning_efforts`
# 时才把字段发出去，档位名对不上就等于没设置。
ReasoningEffort = Literal["low", "high", "max"]


class OutputMode(StrEnum):
    TEXT = "text"
    JSON = "json"


class TaskKind(StrEnum):
    RESUME_PARSE = "resume_parse"
    JD_PARSE = "jd_parse"
    QUERY_REWRITE = "query_rewrite"
    QUERY_PARSE = "query_parse"
    CANDIDATE_PROFILE = "candidate_profile"
    JD_PROFILE = "jd_profile"
    ORG_PARSE = "org_parse"
    LEAD_EXTRACT = "lead_extract"
    MAIL_RESUME_GATE = "mail_resume_gate"
    BD_PLAN = "bd_plan"
    BD_SYNTHESIS = "bd_synthesis"
    OCR = "ocr"
    VISION_PARSE = "vision_parse"
    MATCH_REVIEW = "match_review"
    SEARCH_REVIEW = "search_review"


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    messages: list[dict[str, Any]]
    role: ModelRole
    task_kind: TaskKind
    execution_context: ExecutionContext
    output_mode: OutputMode
    reasoning: ReasoningMode
    reasoning_effort: ReasoningEffort | None = None
    # 「宁可关思考」：能关掉思考的模型一律关掉，`reasoning_effort` 只在**关不掉**
    # （强制思考模型）时才用；AI 设置里按槽位配的强度也不参与。
    # 查询解析走这条路径：实测阿里 qwen3.8-flash 关思考 3.3 秒 / 开思考+low 14.2 秒，
    # 而两档解析出的字段基本一致——把它交给槽位配置等于每次搜索白等十几秒。
    prefer_off: bool = False
    response_model: type[BaseModel] | None = None
    temperature: float | None = None
    deadline_monotonic: float | None = None
    # 输出 token 上界。**不设时一个字段都不发**（保持既有行为）。
    # 实测（2026-09-22）输出长度是延迟的主导因素：同一个模型做「只回一句」的视觉探测要
    # 2.6~5.4 秒，做不约束长度的文本要 73 秒。所以对**输出形状明确**的调用给一个宽松的安全网，
    # 而不是当成硬约束——见 `profile_spec.PROFILE_REWRITE_MAX_TOKENS` 的取值理由。
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AttemptDiagnostic:
    connection_id: str
    provider_id: str
    model: str
    error_code: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    parsed: BaseModel | None
    connection_id: str
    provider_id: str
    model: str
    fallback_used: bool = False
    attempts: tuple[AttemptDiagnostic, ...] = field(default_factory=tuple)


class GenerationClient(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
