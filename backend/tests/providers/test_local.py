import pytest

from kerui_recruit.providers.local import LocalJdParser, LocalResumeParser


@pytest.mark.asyncio
@pytest.mark.parametrize("parser", [LocalResumeParser(), LocalJdParser()])
async def test_local_parsers_ignore_calendar_years_and_unlabelled_numbers(parser) -> None:
    parsed = await (
        parser.parse_resume("2018年加入公司，2023年离职，联系电话 13772185424")
        if isinstance(parser, LocalResumeParser)
        else parser.parse_jd("岗位于 2025年发布，编号 13772185424")
    )

    years = parsed.total_years if isinstance(parser, LocalResumeParser) else parsed.min_years
    assert years is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("具备 5 年工作经验", 5.0),
        ("工作年限：6.5年", 6.5),
        ("8 years of experience building payment systems", 8.0),
    ],
)
async def test_local_resume_parser_extracts_only_labelled_experience_years(
    text: str,
    expected: float,
) -> None:
    parsed = await LocalResumeParser().parse_resume(text)

    assert parsed.total_years == expected

