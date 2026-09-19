from __future__ import annotations

import asyncio

from pydantic import BaseModel


class ResumeGateDecision(BaseModel):
    is_resume: bool
    confidence: float = 0.0


_RESUME_GATE_PROMPT = """你是一名招聘助理，判断下面这封来自白名单邮箱的邮件是否为「求职简历投递」邮件。

判断依据：
- 邮件正文是否像求职者投递简历（如自我介绍、应聘岗位、附上简历等）；
- 附件文件名是否像简历（如「张三-简历.pdf」「个人简历.docx」等）。

请只输出 JSON 对象，字段：
- is_resume：布尔值，true 表示这是一封简历投递邮件，false 表示不是；
- confidence：0~1 的小数，表示判断置信度。

邮件主题：
{subject}

邮件正文：
{body}

附件文件名：
{attachments}"""


class ResumeGate:
    """用大模型判断白名单邮件内容是否为简历投递（同步实现，供邮件轮询线程调用）。"""

    def __init__(self, llm) -> None:
        self._llm = llm

    def is_resume(self, *, subject: str, body: str, attachment_filenames: list[str]) -> bool:
        prompt = _RESUME_GATE_PROMPT.format(
            subject=subject,
            body=body[:2000],
            attachments="、".join(attachment_filenames) if attachment_filenames else "（无附件）",
        )
        try:
            decision = asyncio.run(self._llm.complete_json(
                [{"role": "user", "content": prompt}],
                ResumeGateDecision,
            ))
            return decision.is_resume
        except Exception:
            # 判断失败时保守处理：有简历附件即视为简历，保持现有行为。
            return bool(attachment_filenames)
