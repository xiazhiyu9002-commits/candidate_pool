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
async def test_synthesize_falls_back_on_error() -> None:
    class Boom:
        async def complete_json(self, messages, response_model):
            raise RuntimeError("boom")

    generator = SynthesisGenerator(Boom())  # type: ignore[arg-type]
    out = await generator.synthesize("q", [])
    assert out.leads == []
