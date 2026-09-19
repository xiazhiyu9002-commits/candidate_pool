from __future__ import annotations

import asyncio

from pydantic import BaseModel

from kerui_recruit.bd_search.service import LeadExtractor, LeadInfo, RegexLeadExtractor


class ParsedLead(BaseModel):
    company: str | None = None
    job_title: str | None = None


_LEAD_EXTRACT_PROMPT = """你是招聘领域的线索提取器。

任务：从给定网页的标题和正文中，提取「招聘公司名」和「岗位名称」。

规则：
- company：发布该招聘岗位的公司名；若正文中未明确出现公司名，填 null；
- job_title：岗位名称（如「高级 Java 开发工程师」）；若未出现，填 null；
- 不要臆造，只提取原文明确出现的信息；
- 只输出 JSON 对象，不要输出 markdown 代码块或多余文字。

标题：{title}
正文：{body}"""


class DeepSeekLeadExtractor:
    """Extract company/job from web results using a generation task client.

    Falls back to :class:`RegexLeadExtractor` when the LLM call fails or
    returns nothing, so BD search never breaks on provider errors.
    """

    def __init__(self, llm) -> None:
        self._llm = llm
        self._fallback = RegexLeadExtractor()

    def extract(
        self, title: str, snippet: str, raw_content: str | None = None
    ) -> LeadInfo:
        body = (raw_content or snippet)[:4000]
        try:
            parsed = asyncio.run(self._llm.complete_json(
                [
                    {
                        "role": "user",
                        "content": _LEAD_EXTRACT_PROMPT.format(title=title, body=body),
                    }
                ],
                ParsedLead,
            ))
        except Exception:
            return self._fallback.extract(title, snippet, raw_content)

        if not parsed.company and not parsed.job_title:
            return self._fallback.extract(title, snippet, raw_content)
        return LeadInfo(company=parsed.company, job_title=parsed.job_title)
