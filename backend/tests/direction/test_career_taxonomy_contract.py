"""多值方向的解析契约：词表渲染、限流、镜像与 business 字段上限。"""
from __future__ import annotations

import re

from kerui_recruit.direction.policy import (
    BUSINESS_DIRECTIONS,
    BUSINESS_DIRECTION_LABELS,
    CAREER_DIRECTIONS,
    CAREER_SPECIALIZATIONS,
    SPECIALIZATION_LABELS,
    SPECIALIZATION_PARENT,
)
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.providers import generation_tasks, profile_spec
from kerui_recruit.providers.vision_parse import _VISION_JD_PROMPT, _VISION_RESUME_PROMPT


def test_jd_allows_three_career_directions():
    parsed = ParsedJd(
        title="高级后端",
        career_directions=["BACKEND", "DATA", "OPS", "QA"],
        career_specializations=["BACKEND_SERVICE", "DATA_ANALYSIS", "OPS_SRE", "QA_QUALITY"],
    )
    assert parsed.career_directions == ["BACKEND", "DATA", "OPS"]
    assert parsed.career_specializations == ["BACKEND_SERVICE", "DATA_ANALYSIS", "OPS_SRE"]
    assert parsed.direction == "BACKEND"


def test_jd_caps_business_directions_and_syncs_assessment():
    parsed = ParsedJd(
        title="保险营销平台后端",
        career_directions=["BACKEND"],
        career_specializations=["BACKEND_SERVICE"],
        business_directions=["INSURANCE", "MARKETING", "GAMING"],
        direction_assessment={"primary": "BACKEND", "confidence": "high"},
    )
    assert parsed.business_directions == ["INSURANCE", "MARKETING"]
    assert parsed.career_taxonomy_version == "4"
    assert parsed.direction_assessment["business_directions"] == ["INSURANCE", "MARKETING"]


def test_jd_specializations_must_belong_to_selected_directions():
    parsed = ParsedJd(
        title="数据开发",
        career_directions=["DATA"],
        career_specializations=["BACKEND_SERVICE", "DATA_WAREHOUSE"],
    )
    assert parsed.career_specializations == ["DATA_WAREHOUSE"]


def test_resume_prompt_renders_full_taxonomy():
    prompt = generation_tasks._RESUME_PARSE_PROMPT
    assert "{career_taxonomy}" in prompt and "{business_taxonomy}" in prompt
    rendered = generation_tasks._CAREER_TAXONOMY
    for direction in CAREER_DIRECTIONS:
        assert direction in rendered
    for spec in CAREER_SPECIALIZATIONS:
        assert spec in rendered
        assert SPECIALIZATION_PARENT[spec] in rendered
        assert SPECIALIZATION_LABELS[spec] in rendered


def test_jd_prompt_renders_full_taxonomy():
    prompt = generation_tasks._JD_PARSE_PROMPT
    assert "{career_taxonomy}" in prompt and "{business_taxonomy}" in prompt
    rendered = generation_tasks._BUSINESS_TAXONOMY
    for code in BUSINESS_DIRECTIONS:
        assert code in rendered
        assert BUSINESS_DIRECTION_LABELS[code] in rendered


def test_prompts_carry_strictness_rules_and_no_legacy_specializations():
    for prompt in (generation_tasks._RESUME_PARSE_PROMPT, generation_tasks._JD_PARSE_PROMPT):
        assert "禁止只凭职位名称判定方向" in prompt
        assert "放宽数量不等于放宽标准" in prompt
        assert 'taxonomy_version（固定 "4"）' in prompt
        # 旧提示词里的单值方向与旧专长枚举不应残留
        assert "FULL_STACK=" not in prompt
        assert "DATA_PLATFORM=" not in prompt
        assert 'taxonomy_version（固定 "3"）' not in prompt


def test_prompts_format_without_leftover_placeholders():
    """格式化后不得残留未替换的花括号占位符（否则会原样发给模型）。"""
    resume_prompt = generation_tasks.render_resume_parse_prompt("简历正文")
    jd_prompt = generation_tasks.render_jd_parse_prompt("JD 正文")
    for prompt in (resume_prompt, jd_prompt):
        assert not re.search(r"\{[a-z_]+\}", prompt), prompt
        assert "career_directions" in prompt
        assert "business_directions" in prompt
    # 正文确实被注入到末尾输入段。
    assert "简历正文" in resume_prompt and "JD 正文" in jd_prompt
    # 硬条件口径必须真的进了 JD 提示词，而不是留着占位符。
    assert "exact_constraints" in jd_prompt
    assert "只有原文明确写" in jd_prompt


def test_vision_prompts_render_all_placeholders():
    """视觉解析提示词同样要渲染：曾经直接把未格式化的模板发给模型，词表与字数口径全丢。"""
    for prompt in (_VISION_RESUME_PROMPT, _VISION_JD_PROMPT):
        assert not re.search(r"\{[a-z_]+\}", prompt), prompt
        assert "职业方向词表" in prompt and "业务方向词表" in prompt
        assert "请根据下方图片" in prompt
        assert profile_spec.COMPACT_LENGTH in prompt
    assert "exact_constraints" in _VISION_JD_PROMPT
    assert "只有原文明确写" in _VISION_JD_PROMPT
    assert profile_spec.CANDIDATE_INLINE_RULE in _VISION_RESUME_PROMPT
