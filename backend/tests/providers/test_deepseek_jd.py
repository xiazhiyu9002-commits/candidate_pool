import pytest

from kerui_recruit.jd.structured import ParsedJd, ParsedJdRequirement
from kerui_recruit.providers.generation_tasks import AiJdParser


class FakeLlm:
    def __init__(self, parsed) -> None:
        self.parsed = parsed
        self.captured_messages: list | None = None

    async def complete_json(self, messages, response_model, temperature=None, **kwargs):
        self.captured_messages = messages
        return self.parsed


@pytest.mark.asyncio
async def test_deepseek_jd_parser_maps_json_to_parsed_jd() -> None:
    llm = FakeLlm(ParsedJd(
        title="Java 后端工程师",
        company="某金融科技",
        department="支付",
        location="北京",
        salary="30-50K",
        ai_category="AI_RELATED",
        industry="金融",
        min_years=3.0,
        highest_degree="本科",
        summary="负责支付系统后端开发",
        requirements=[
            ParsedJdRequirement(kind="MUST", label="技能", value="Java"),
            ParsedJdRequirement(kind="PLUS", label="行业", value="金融"),
        ],
    ))
    parser = AiJdParser(llm)

    result = await parser.parse_jd("某金融科技招聘 Java 后端，3年，本科，金融支付")

    assert isinstance(result, ParsedJd)
    assert result.title == "Java 后端工程师"
    assert result.min_years == 3.0
    assert result.ai_category == "AI_RELATED"
    assert result.requirements[0] == ParsedJdRequirement(kind="MUST", label="技能", value="Java")


@pytest.mark.asyncio
async def test_deepseek_jd_split_formats_prompt_without_brace_error() -> None:
    from kerui_recruit.providers.generation_tasks import JdSplit

    llm = FakeLlm(JdSplit(chunks=["测试岗位 JD"]))
    parser = AiJdParser(llm)

    chunks = await parser.split_jds("测试岗位 JD")
    assert chunks == ["测试岗位 JD"]
    assert llm.captured_messages is not None
    assert '"chunks"' in llm.captured_messages[0]["content"]
