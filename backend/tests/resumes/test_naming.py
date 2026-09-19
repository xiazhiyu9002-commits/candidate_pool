from decimal import Decimal

from kerui_recruit.resumes.pipeline import build_display_name
from kerui_recruit.resumes.structured import (
    NormalizedExperience,
    NormalizedProject,
    NormalizedResume,
)


def _resume() -> NormalizedResume:
    return NormalizedResume(
        name="张三",
        total_years=Decimal("6"),
        highest_degree="硕士",
        location="上海",
        skills=("Java", "金融风控", "Python"),
        summary="金融科技后端",
        experiences=(),
        projects=(),
    )


def test_build_display_name_uses_parsed_fields() -> None:
    name = build_display_name(_resume(), ".pdf")
    assert name.startswith("张三")
    assert "6年" in name
    assert "硕" in name
    assert "Java" in name
    assert name.endswith(".pdf")


def test_build_display_name_handles_missing_fields() -> None:
    resume = NormalizedResume(
        name="李四",
        total_years=None,
        highest_degree=None,
        location=None,
        skills=(),
        summary="",
        experiences=(),
        projects=(),
    )
    assert build_display_name(resume, ".pdf") == "李四.pdf"


def test_build_display_name_uses_placeholder_when_name_missing() -> None:
    """姓名缺失时用中性占位，不能退化成空串或文件名。"""
    resume = NormalizedResume(
        name=None,
        total_years=Decimal("6"),
        highest_degree="硕士",
        location=None,
        skills=(),
        summary="",
        experiences=(),
        projects=(),
    )
    assert build_display_name(resume, ".pdf") == "未知-6年-硕.pdf"
