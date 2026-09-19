"""生成式 AI 稳定公共接口：角色、执行场景、统一请求/响应与客户端协议。

业务层只声明能力与场景，不依赖供应商名或模型名。
"""
from kerui_recruit.providers.ai.contracts import (
    AttemptDiagnostic,
    ExecutionContext,
    GenerationClient,
    GenerationRequest,
    GenerationResult,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)

__all__ = [
    "AttemptDiagnostic",
    "ExecutionContext",
    "GenerationClient",
    "GenerationRequest",
    "GenerationResult",
    "ModelRole",
    "OutputMode",
    "ReasoningMode",
    "TaskKind",
]
