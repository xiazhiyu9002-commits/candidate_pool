"""Deprecated DeepSeek 专用解析类。

供应商无关的解析实现与提示词已迁至 :mod:`kerui_recruit.providers.generation_tasks`。
本模块仅为兼容保留再导出；新代码应直接使用 ``AiResumeParser`` / ``AiJdParser``。
"""
from __future__ import annotations

from kerui_recruit.providers.generation_tasks import (
    AiJdParser,
    AiResumeParser,
    JdSplit,
    _JD_PARSE_PROMPT,
    _JD_SPLIT_PROMPT,
    _RESUME_PARSE_PROMPT,
)

__all__ = [
    "AiJdParser",
    "AiResumeParser",
    "JdSplit",
    "DeepSeekResumeParser",
    "DeepSeekJdParser",
    "_JD_PARSE_PROMPT",
    "_JD_SPLIT_PROMPT",
    "_RESUME_PARSE_PROMPT",
]


class DeepSeekResumeParser(AiResumeParser):
    """Deprecated; use :class:`AiResumeParser`."""


class DeepSeekJdParser(AiJdParser):
    """Deprecated; use :class:`AiJdParser`."""
