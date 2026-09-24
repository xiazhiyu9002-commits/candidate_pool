from __future__ import annotations

from kerui_recruit.providers.generation_tasks import (
    _MAX_MODEL_INPUT_CHARS,
    render_jd_parse_prompt,
    render_resume_parse_prompt,
    truncate_model_input,
)


def test_short_text_is_untouched() -> None:
    text = "张三 6 年后端经验"
    assert truncate_model_input(text) == text


def test_long_text_keeps_head_and_tail_with_explicit_marker() -> None:
    """超长正文必须保留首尾，并显式声明省略了多少字符。

    简历的姓名/概述与最近经历在开头，尾部是更早的经历，两端都不能丢；
    显式标记则避免模型把省略号当原文。
    """
    head_marker = "HEAD-起始标记"
    tail_marker = "TAIL-结尾标记"
    text = head_marker + ("中" * (_MAX_MODEL_INPUT_CHARS * 2)) + tail_marker

    truncated = truncate_model_input(text)

    assert truncated.startswith(head_marker)
    assert truncated.endswith(tail_marker)
    # 省略的字符数必须如实写出，便于排查「为什么模型没看到某段经历」。
    assert f"此处省略 {len(text) - _MAX_MODEL_INPUT_CHARS} 个字符" in truncated


def test_truncation_respects_custom_limit() -> None:
    truncated = truncate_model_input("A" * 100 + "B" * 100, limit=50)
    # 保留的正文正好是「头 35 + 尾 15」，中间被省略标记替代（标记本身不计入上限）。
    assert truncated.startswith("A" * 35)
    assert truncated.endswith("B" * 15)


def test_render_resume_parse_prompt_truncates_body() -> None:
    prompt = render_resume_parse_prompt("简" * (_MAX_MODEL_INPUT_CHARS + 500))
    assert "此处省略" in prompt
    assert "简历原文" in prompt


def test_render_jd_parse_prompt_truncates_body() -> None:
    prompt = render_jd_parse_prompt("职" * (_MAX_MODEL_INPUT_CHARS + 500))
    assert "此处省略" in prompt
    assert "JD 原文" in prompt


def test_render_prompts_keep_normal_body_verbatim() -> None:
    """正常长度的正文必须逐字进入提示词，截断不能误伤。"""
    resume_prompt = render_resume_parse_prompt("张三 6年后端")
    jd_prompt = render_jd_parse_prompt("招聘后端工程师")
    assert "张三 6年后端" in resume_prompt
    assert "招聘后端工程师" in jd_prompt
    assert "此处省略" not in resume_prompt
    assert "此处省略" not in jd_prompt
