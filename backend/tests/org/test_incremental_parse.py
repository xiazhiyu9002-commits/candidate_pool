from __future__ import annotations

import pytest

from kerui_recruit.org.import_parser import _chunk_text, DeepSeekOrgImportParser
from kerui_recruit.org.structured import (
    OrgClarificationQuestion,
    OrgParseResult,
    ParsedOrgDraft,
)


def test_chunk_text_returns_short_text_as_single_chunk():
    assert _chunk_text("短文本", max_chars=10) == ["短文本"]


def test_chunk_text_splits_on_blank_lines():
    text = "段落一。\n\n段落二。\n\n段落三。"
    assert _chunk_text(text, max_chars=100) == ["段落一。", "段落二。", "段落三。"]


def test_chunk_text_splits_long_paragraph_at_sentence_endings():
    text = "句子一。" * 100  # 400 字，单段超长
    chunks = _chunk_text(text, max_chars=50)
    assert len(chunks) > 1
    # 每段以句末标点结尾，不切断句子
    assert all(chunk.endswith(("。", "；", "！", "？")) for chunk in chunks)


class FakeLLM:
    def __init__(self, responses: list[OrgParseResult]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(self, messages, response_model):
        self.calls.append(messages)
        if not self.responses:
            return OrgParseResult(draft=ParsedOrgDraft(company_name="未命名公司"), questions=[])
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _make_parser(responses: list[OrgParseResult]) -> DeepSeekOrgImportParser:
    return DeepSeekOrgImportParser(FakeLLM(responses))


@pytest.mark.asyncio
async def test_parse_incremental_calls_llm_per_chunk_and_merges():
    text = "第一段。\n\n第二段。"
    draft_first = ParsedOrgDraft(company_name="未命名公司")
    draft_second = ParsedOrgDraft(company_name="得物")
    question = OrgClarificationQuestion(question="公司名是什么？", field="company_name", hint=None)

    parser = _make_parser([
        OrgParseResult(draft=draft_first, questions=[question]),
        OrgParseResult(draft=draft_second, questions=[]),
    ])

    result = await parser.parse_incremental(text)

    assert len(parser._llm.calls) == 2  # type: ignore[union-attr]
    assert result.draft is not None
    assert result.draft.company_name == "得物"
    assert len(result.questions) == 1
    assert result.questions[0].question == "公司名是什么？"


@pytest.mark.asyncio
async def test_parse_clarifying_switches_to_incremental_for_long_text():
    long_text = "句子一。" * 800  # 3200 字，超过阈值
    draft = ParsedOrgDraft(company_name="得物")

    parser = _make_parser([OrgParseResult(draft=draft, questions=[])])
    result = await parser.parse_clarifying(long_text)

    assert result.draft is not None
    assert result.draft.company_name == "得物"
    assert len(parser._llm.calls) > 1  # type: ignore[union-attr]
