from __future__ import annotations

from pydantic import BaseModel

from kerui_recruit.bd_agent.evidence import RankedChunk


class EvidenceItem(BaseModel):
    claim: str | None = None
    quote: str | None = None
    source_url: str | None = None


class SynthesizedLead(BaseModel):
    company: str | None = None
    job_title: str | None = None
    is_hiring: bool | None = None
    confidence: float | None = None
    posted_time: str | None = None
    salary_range: str | None = None
    level: str | None = None
    requirements: list[str] = []
    summary: str | None = None
    evidence: list[EvidenceItem] = []


class SynthesisResult(BaseModel):
    leads: list[SynthesizedLead] = []
    needs_more_search: bool = False
    follow_up_queries: list[str] = []


def _format_chunks(chunks: list[RankedChunk]) -> str:
    lines = []
    for index, chunk in enumerate(chunks, start=1):
        # 标题必须带上：招聘站正文通常只有岗位职责，雇主名在页面标题里
        # （「XX科技招聘算法工程师-北京-BOSS直聘」）。丢掉标题会让模型只能填 null。
        header = f"[{index}] 来源: {chunk.source_url}"
        if chunk.title:
            header = f"[{index}] 标题: {chunk.title}\n来源: {chunk.source_url}"
        lines.append(f"{header}\n{chunk.text}")
    return "\n\n".join(lines) or "（无证据）"


_SYNTHESIS_PROMPT = """你是招聘领域的信息综合器。

任务：根据给定的证据片段，回答用户查询，整理出招聘线索。

输出一个 JSON 对象，字段如下：
- leads：线索数组，每项含：
  - company：招聘公司名。**优先从证据的「标题」里取**——招聘页标题常形如「岗位名-公司名-地点」或「公司名招聘岗位名」，
    标题里出现的公司名属于明确信息，不算臆造；标题里没有再看正文。都没有才填 null；
  - job_title：岗位名称（未明确则 null）；
  - is_hiring：是否在招聘（true/false/null，证据不足填 null）；
  - confidence：0~1 的可信度；
  - posted_time：岗位发布时间/开放时间（如"3天前""2026-08-01""12小时前"，未明确则 null）；
  - salary_range：薪资范围（如"50-80K""1.4-2.5万""面议"，未明确则 null）；
  - level：职级/等级（如"P7""高级""总监""应届"，未明确则 null）；
  - requirements：岗位要求数组（学历、工作年限、技能等硬性要求，逐条列出，可为空数组）；
  - summary：一句话概括该线索；
  - evidence：证据数组，每项含 claim（断言）、quote（原文片段）、source_url（来源URL）；
- needs_more_search：布尔，若关键信息（是否在招/岗位）证据不足填 true；
- follow_up_queries：若 needs_more_search 为 true，给出最多 2 条补充搜索式。

规则：
- leads 数组最多返回 20 条不重复线索（公司+岗位去重），不要虚构凑数；
- 只从证据中提取信息，不得臆造公司、岗位、薪资、时间或"一定在招"的结论；
- 证据的「标题」与正文同等可信：公司在标题或正文任一处出现过就必须提取，不得因为只出现在标题里而填 null；
- 若证据明确显示岗位已关闭、已招满、招聘已结束或已过期（如"已停止招聘""招满""招聘结束"等字样），则丢弃该线索，不要输出；
- 招聘岗位页/职位发布页本身即视为"在招"的正面证据，默认 is_hiring 填 true；仅当证据明确显示已关闭时才填 false；
- 证据不足以判断在招状态时 is_hiring 填 null，但仍保留该线索供人工核验；
- 每条结论必须用 evidence 引用原文，quote 必须逐字来自证据；
- 只输出 JSON 对象，不要输出 markdown 或多余文字。

用户查询：{query}

证据片段：
{chunks}"""


class SynthesisGenerator:
    """Synthesize cited BD leads from ranked evidence chunks using an LLM."""

    def __init__(self, llm) -> None:
        self._llm = llm

    async def synthesize(
        self,
        query: str,
        chunks: list[RankedChunk],
    ) -> SynthesisResult:
        """综合证据片段为带引用的线索。

        这里**不吞异常**。上游失败必须让调用方看见：一旦在本地把异常变成空结果，
        「模型不可用」就会伪装成「搜索没找到线索」，前端于是显示「暂无线索」而不是报错。
        实测 BD 助手连续 14 轮静默返回 0 条线索就是这么来的。
        降级由 `BdAgent._synthesis_step` 统一处理，并把原因一路带到界面。
        """
        return await self._llm.complete_json(
            messages=[
                {
                    "role": "user",
                    "content": _SYNTHESIS_PROMPT.format(
                        query=query,
                        chunks=_format_chunks(chunks),
                    ),
                }
            ],
            response_model=SynthesisResult,
        )
