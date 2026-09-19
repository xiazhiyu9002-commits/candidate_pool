"""AI 深度复核核心逻辑单测：prompt 构造与结构化结论校验。"""
from __future__ import annotations

import pytest

from kerui_recruit.match.review import (
    ReviewVerdictModel,
    build_review_prompt,
    validated_verdict,
)


def test_build_review_prompt_contains_jd_and_candidate():
    prompt = build_review_prompt("支付高并发服务", "搭建支付高并发服务")
    assert "支付高并发服务" in prompt
    assert "搭建支付高并发服务" in prompt


def _validated(model):
    return validated_verdict(model)


def test_validated_accepts_recommend_with_reasons_and_cautions():
    result = _validated(ReviewVerdictModel(verdict="recommend", reasons=["项目匹配"], cautions=["缺少某经验"]))
    assert result["verdict"] == "recommend"
    assert result["reasons"] == ("项目匹配",)
    assert result["cautions"] == ("缺少某经验",)


def test_validated_rejects_invalid_verdict():
    with pytest.raises(ValueError):
        _validated(ReviewVerdictModel(verdict="invented", reasons=[], cautions=[]))


def test_validated_filters_blank_reasons():
    result = _validated(ReviewVerdictModel(verdict="pending", reasons=["", "  "], cautions=[]))
    assert result["reasons"] == ()
    assert result["cautions"] == ()
