from __future__ import annotations

from dataclasses import dataclass

from kerui_recruit.resumes.quality import analyze_text
from kerui_recruit.resumes.structured import ParsedResume


@dataclass(frozen=True, slots=True)
class ValidityResult:
    ok: bool
    error_code: str | None
    reason: str


def check_parsed_resume(parsed: ParsedResume, source_text: str) -> ValidityResult:
    """判断结构化结果是否达到「可写入画像并进入索引」的最低要求。

    JSON 结构合法不等于解析有效：几乎全空、只有水印或无效姓名的结果必须被
    拒绝，同时不要求所有简历都具备姓名/电话/工作经历（匿名、应届生也要能通过）。
    """
    signals = _meaningful_signal_count(parsed)
    # 关键内容信号：技能 / 有意义的个人总结 / 有内容的工作经历，至少两项，
    # 避免「只解析到学历或学校」这类近乎空壳的结果通过。
    profile_content_signals = (
        _nonempty(parsed.skills),
        _is_meaningful_summary(parsed.summary),
        any(_has_experience_content(e) for e in parsed.experiences),
    )
    has_profile_content = sum(profile_content_signals) >= 2
    if signals >= 3 and has_profile_content:
        return ValidityResult(ok=True, error_code=None, reason="解析结果有效")

    # 统一归为「不合格」：不再区分「请人工复核」与「结构化为空」，
    # 两者都通过「重新解析」（视觉模型）补救，前端只提供查看原文 + 重新解析入口。
    return ValidityResult(
        ok=False,
        error_code="E_PARSE_INCOMPLETE",
        reason="解析结果不完整，已归为不合格，可重新解析",
    )


def _meaningful_signal_count(parsed: ParsedResume) -> int:
    signals = 0
    if _is_name_like(parsed.name):
        signals += 1
    if parsed.total_years is not None:
        signals += 1
    if _nonempty(parsed.highest_degree):
        signals += 1
    if _nonempty(parsed.location):
        signals += 1
    if _nonempty(parsed.school):
        signals += 1
    if any(_nonempty(v) for v in (parsed.industry, parsed.current_industry, parsed.longest_industry)):
        signals += 1
    if _nonempty(parsed.skills):
        signals += 1
    if _is_meaningful_summary(parsed.summary):
        signals += 1
    if any(_has_experience_content(e) for e in parsed.experiences):
        signals += 1
    if any(_has_project_content(p) for p in parsed.projects):
        signals += 1
    return signals


def _is_name_like(name: str | None) -> bool:
    if not name:
        return False
    value = " ".join(name.split())
    if not (2 <= len(value) <= 30):
        return False
    quality = analyze_text(value)
    return (
        quality.valid_char_count >= 2
        and quality.dominant_ratio == 0.0
        and quality.repeated_ratio == 0.0
    )


def _is_meaningful_summary(summary: str | None) -> bool:
    if not summary:
        return False
    value = " ".join(summary.split())
    if len(value) < 10:
        return False
    return analyze_text(summary).meaningful


def _nonempty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(_nonempty(item) for item in value)
    return bool(" ".join(str(value).split()))


def _has_experience_content(experience) -> bool:
    return any(
        _nonempty(value)
        for value in (experience.company, experience.title, experience.summary)
    )


def _has_project_content(project) -> bool:
    return any(
        _nonempty(value)
        for value in (project.name, project.summary, project.tech_stack, project.business_scene)
    )
