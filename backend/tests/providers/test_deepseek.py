from __future__ import annotations

import pytest

from kerui_recruit.providers.generation_tasks import AiResumeParser
from kerui_recruit.resumes.structured import ParsedResume


class FakeLlm:
    def __init__(self, parsed: ParsedResume) -> None:
        self.parsed = parsed
        self.captured_messages: list | None = None

    async def complete_json(self, messages, response_model, temperature=None, **kwargs):
        self.captured_messages = messages
        return self.parsed


@pytest.mark.asyncio
async def test_deepseek_parser_maps_json_to_parsed_resume() -> None:
    llm = FakeLlm(ParsedResume(
        name="张三",
        total_years=6.0,
        highest_degree="硕士",
        location="上海",
        skills=["Python", "Java"],
        summary="金融风控",
        experiences=[{"company": "A", "title": "工程师", "summary": "支付"}],
        projects=[{"name": "P", "summary": "结算"}],
    ))
    parser = AiResumeParser(llm)

    result = await parser.parse_resume("张三 6年 硕士 Python")

    assert isinstance(result, ParsedResume)
    assert result.name == "张三"
    assert result.total_years == 6.0
    assert result.skills == ["Python", "Java"]
    assert result.experiences[0].company == "A"
    assert llm.captured_messages is not None
    assert "张三 6年 硕士 Python" in llm.captured_messages[0]["content"]
