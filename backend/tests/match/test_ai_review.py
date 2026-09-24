"""AI 深度复核核心逻辑单测：输入源选择、小节切分与裁剪、三段契约与三档兜底。"""
from __future__ import annotations

import pytest

from kerui_recruit.match.review import (
    REVIEW_OUTPUT_CONTRACT,
    ReviewVerdictModel,
    apply_consistency_fallback,
    build_basis,
    build_evidence_section,
    build_review_prompt,
    render_jd_sections,
    resolve_jd_source,
    segment_jd_text,
    trim_sections,
    validated_verdict,
)

_JD_TEXT = (
    "岗位职责：\n"
    "1. 负责支付核心链路的稳定性建设；\n"
    "2. 主导交易系统的容量规划。\n"
    "优先项：\n"
    "有支付行业背景优先。\n"
    "任职要求：\n"
    "1. 熟悉 Java 与 Spring；\n"
    "2. 本科及以上学历。\n"
    "福利待遇：\n"
    "五险一金、弹性工作。\n"
)


def test_build_review_prompt_contains_jd_and_candidate():
    prompt = build_review_prompt("支付高并发服务", "搭建支付高并发服务")
    assert "支付高并发服务" in prompt
    assert "搭建支付高并发服务" in prompt
    # 三段顺序与 JSON 契约必须在提示词里可见。
    assert "project_match" in prompt
    assert "experience_match" in prompt
    assert "tech_match" in prompt
    assert REVIEW_OUTPUT_CONTRACT in prompt


def _validated(model):
    return validated_verdict(model)


def test_validated_keeps_three_sections_in_order():
    result = _validated(ReviewVerdictModel(
        verdict="recommend",
        project_match=["做过支付网关（projects[0]）"],
        experience_match=["工作经历与岗位同类（experiences[0]）"],
        tech_match=["Java 主干对得上（skills）"],
        risks=["未见高并发容量规划（experiences[1]）"],
    ))
    assert result["verdict"] == "recommend"
    assert result["project_match"] == ("做过支付网关（projects[0]）",)
    assert result["experience_match"] == ("工作经历与岗位同类（experiences[0]）",)
    assert result["tech_match"] == ("Java 主干对得上（skills）",)
    assert result["risks"] == ("未见高并发容量规划（experiences[1]）",)


def test_validated_rejects_invalid_verdict():
    with pytest.raises(ValueError):
        _validated(ReviewVerdictModel(verdict="invented"))


def test_validated_filters_blank_lines():
    result = _validated(ReviewVerdictModel(
        verdict="pending", project_match=["", "  "], risks=["", "缺经验"],
    ))
    assert result["project_match"] == ()
    assert result["risks"] == ("缺经验",)


def test_evidence_section_renders_labels_and_handles_empty():
    section = build_evidence_section({"overview": "整体画像", "tech": "技术片段", "business": "业务片段"})
    assert "整体概况：整体画像" in section
    assert "技术最相关片段：技术片段" in section
    assert "业务最相关片段：业务片段" in section
    assert build_evidence_section({}) == ""
    assert build_evidence_section(None) == ""


# ---- 小节切分与渲染 ----


def test_segment_jd_text_labels_known_headings():
    sections = {section.label: section.text for section in segment_jd_text(_JD_TEXT)}

    assert "负责支付核心链路的稳定性建设" in sections["岗位职责"]
    assert "有支付行业背景优先" in sections["优先项"]
    assert "熟悉 Java 与 Spring" in sections["任职要求"]
    assert "五险一金" in sections["其他"]


def test_render_jd_sections_adds_weight_labels():
    rendered = render_jd_sections(segment_jd_text(_JD_TEXT))

    assert "【岗位职责】" in rendered
    assert "【优先项】" in rendered
    assert "【任职要求】" in rendered
    # 无关小节不加标签（不做权重暗示），但内容保留。
    assert "五险一金" in rendered


def test_segment_without_headings_returns_untagged_block():
    sections = segment_jd_text("我们要招一个后端工程师，负责支付链路。")

    assert len(sections) == 1
    assert sections[0].label == "其他"
    # 没有标题信息时不能凭空标出权重。
    assert render_jd_sections(sections) == "我们要招一个后端工程师，负责支付链路。"


def test_trim_keeps_plus_section_and_cuts_on_line_boundary():
    long_text = "\n".join(
        ["优先项：", "有支付行业背景优先。", "任职要求："]
        + [f"第{i}条要求，熟悉若干具体技术名词与工程实践。" for i in range(300)]
    )
    sections, trimmed = trim_sections(segment_jd_text(long_text), max_chars=500)

    assert trimmed is not None
    assert trimmed["before"] > 500
    assert trimmed["after"] <= 500
    rendered = render_jd_sections(sections)
    assert "有支付行业背景优先。" in rendered  # 优先项永不丢弃
    assert all(line.endswith("。") for line in rendered.splitlines() if line.strip())  # 没截半句


def test_trim_is_noop_for_short_text():
    sections = segment_jd_text(_JD_TEXT)
    kept, trimmed = trim_sections(sections)

    assert trimmed is None
    assert kept == sections


# ---- 输入源选择 ----


def test_resolve_jd_source_prefers_manually_edited_profile():
    text, source, trimmed = resolve_jd_source(
        {"candidate_profile": "目标人选：有支付高并发经验的后端"},
        _JD_TEXT,
        {"candidate_profile": "目标人选：有支付高并发经验的后端"},
    )

    assert source == "profile"
    assert text == "目标人选：有支付高并发经验的后端"
    assert trimmed is None


def test_resolve_jd_source_falls_back_to_annotated_source_text():
    text, source, _ = resolve_jd_source({"candidate_profile": "画像"}, _JD_TEXT)

    assert source == "source_text"
    assert "【岗位职责】" in text
    assert "【优先项】" in text


def test_resolve_jd_source_falls_back_to_parsed_data():
    text, source, _ = resolve_jd_source({"core_duties": ["支付高并发服务"]}, None)

    assert source == "parsed"
    assert "支付高并发服务" in text


# ---- 一致性兜底 ----


def test_consistency_fallback_downgrades_recommend_without_evidence():
    verdict, basis = apply_consistency_fallback(
        {"verdict": "recommend", "project_match": (), "experience_match": (), "tech_match": ()},
        build_basis(source="source_text", eligible=True),
    )

    assert verdict["verdict"] == "pending"
    assert basis["degraded"] == "no_evidence"


def test_consistency_fallback_marks_reject_with_evidence():
    verdict, basis = apply_consistency_fallback(
        {"verdict": "reject", "project_match": ["有证据"], "experience_match": (), "tech_match": ()},
        build_basis(source="source_text", eligible=True),
    )

    assert verdict["verdict"] == "reject"
    assert basis["degraded"] == "reject_with_evidence"
    assert basis["matched_dimensions"] == ["project_match"]


def test_consistency_fallback_keeps_recommend_with_evidence():
    verdict, basis = apply_consistency_fallback(
        {"verdict": "recommend", "project_match": ["有证据"], "experience_match": (), "tech_match": ()},
        build_basis(source="profile", eligible=True),
    )

    assert verdict["verdict"] == "recommend"
    assert basis["degraded"] is None


def test_consistency_fallback_leaves_failed_entries_alone():
    failed = {"verdict": "pending", "failed": True, "risks": ("复核超时",)}
    verdict, basis = apply_consistency_fallback(failed, build_basis(source="search", eligible=True))

    assert verdict is failed
    assert basis["degraded"] is None
