from __future__ import annotations

import pytest

from kerui_recruit.bd_agent.evidence import RankedChunk
from kerui_recruit.bd_agent.synthesis import (
    EvidenceItem,
    SynthesisGenerator,
    SynthesisResult,
    SynthesizedLead,
)


class FakeLLM:
    def __init__(self, result: SynthesisResult) -> None:
        self._result = result

    async def complete_json(self, messages, response_model):
        return self._result


@pytest.mark.asyncio
async def test_synthesize_returns_leads() -> None:
    result = SynthesisResult(
        leads=[
            SynthesizedLead(
                company="A公司",
                job_title="工程师",
                is_hiring=True,
                confidence=0.9,
                evidence=[
                    EvidenceItem(claim="在招", quote="原文", source_url="https://a.com")
                ],
            )
        ]
    )
    generator = SynthesisGenerator(FakeLLM(result))
    out = await generator.synthesize(
        "query", [RankedChunk(text="chunk", source_url="https://a.com")]
    )
    assert out.leads[0].company == "A公司"
    assert out.leads[0].evidence[0].source_url == "https://a.com"


@pytest.mark.asyncio
async def test_synthesize_prompt_carries_chunk_title() -> None:
    """标题必须进提示词。

    招聘站的正文通常只有岗位职责，雇主名在页面标题里。提示词里看不到标题，
    模型只能按「未明确则 null、不得臆造」填 null，线索就落成「未识别公司」。
    """
    captured: list[str] = []

    class Capturing:
        async def complete_json(self, messages, response_model):
            captured.append(messages[0]["content"])
            return SynthesisResult()

    generator = SynthesisGenerator(Capturing())  # type: ignore[arg-type]
    await generator.synthesize(
        "算法工程师",
        [
            RankedChunk(
                text="负责推荐系统召回与排序",
                source_url="https://www.zhipin.com/job/1",
                title="XX科技招聘算法工程师-北京",
            )
        ],
    )

    assert "XX科技招聘算法工程师-北京" in captured[0]
    assert "https://www.zhipin.com/job/1" in captured[0]


@pytest.mark.asyncio
async def test_synthesize_prompt_omits_missing_title() -> None:
    """没有标题时不渲染空的「标题:」行，避免模型把空值当成证据。"""
    captured: list[str] = []

    class Capturing:
        async def complete_json(self, messages, response_model):
            captured.append(messages[0]["content"])
            return SynthesisResult()

    generator = SynthesisGenerator(Capturing())  # type: ignore[arg-type]
    await generator.synthesize("q", [RankedChunk(text="正文", source_url="https://a.com")])

    assert "标题:" not in captured[0]


@pytest.mark.asyncio
async def test_synthesize_propagates_error() -> None:
    """上游失败必须抛出来，不能在本层变成空结果。

    一旦在本地吞掉，「模型不可用」就会伪装成「搜索没找到线索」，
    前端只能显示「暂无线索」——实测 BD 助手连续 14 轮静默返回 0 条线索就是这么来的。
    降级与原因记录由 `BdAgent._synthesis_step` 统一负责。
    """

    class Boom:
        async def complete_json(self, messages, response_model):
            raise RuntimeError("boom")

    generator = SynthesisGenerator(Boom())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="boom"):
        await generator.synthesize("q", [])
