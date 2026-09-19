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


class OutputMode(StrEnum):
    TEXT = "text"
    JSON = "json"


class TaskKind(StrEnum):
    RESUME_PARSE = "resume_parse"
    JD_PARSE = "jd_parse"
    QUERY_REWRITE = "query_rewrite"
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
    reasoning_effort: Literal["low", "high", "max"] | None = None
    response_model: type[BaseModel] | None = None
    temperature: float | None = None
    deadline_monotonic: float | None = None


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
