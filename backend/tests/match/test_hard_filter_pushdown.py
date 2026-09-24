"""JD 硬条件下推到检索层的单元测试（方案 §2）。

只测纯函数映射与筛选口径；端到端的「安全阀回退」由匹配服务测试覆盖。
"""
from kerui_recruit.match.service import (
    _constraint_summary,
    _hard_filter_pushdown,
    _must_constraints,
)


def _constraint(kind: str, alternatives: list[str], strength: str = "MUST",
                source_text: str = "必须满足") -> dict:
    return {"kind": kind, "operator": "OR", "alternatives": alternatives,
            "strength": strength, "source": "inferred", "source_text": source_text}


def test_must_constraints_require_source_text() -> None:
    """没有原文依据的 MUST 不具备淘汰力（与 normalize_constraints 的口径一致）。"""
    parsed = {"exact_constraints": [
        _constraint("company_history", ["字节"]),
        _constraint("company_history", ["阿里"], source_text=""),
        _constraint("company_history", ["腾讯"], strength="PLUS"),
    ]}
    accepted = _must_constraints(parsed)
    assert len(accepted) == 1
    assert accepted[0]["alternatives"] == ["字节"]


def test_school_level_pushdown_picks_loosest_alternative() -> None:
    """OR 语义下必须取最宽松档：985 -> {985}，211 -> {211,985}，双一流更宽。

    取最严格档会把「985 或 211」误变成「必须 985」—— 这是最危险的错误方向。
    """
    parsed = {"exact_constraints": [_constraint("school_level", ["985", "211"])]}
    assert _hard_filter_pushdown(parsed)["school_level"] == "211"

    parsed = {"exact_constraints": [_constraint("school_level", ["985"])]}
    assert _hard_filter_pushdown(parsed)["school_level"] == "985"


def test_company_history_pushdown_becomes_multi_value_or() -> None:
    parsed = {"exact_constraints": [_constraint("company_history", ["字节", "阿里", "字节"])]}
    assert _hard_filter_pushdown(parsed)["companies"] == ("字节", "阿里")


def test_degree_pushdown_picks_loosest_level() -> None:
    """学历是「最低层级」语义：多值 OR 时取最低那档（本科 ⊇ 硕士）。"""
    parsed = {"exact_constraints": [_constraint("degree", ["本科", "硕士"])]}
    assert _hard_filter_pushdown(parsed)["highest_degree"] == "BACHELOR"

    parsed = {"exact_constraints": [_constraint("degree", ["硕士"])]}
    assert _hard_filter_pushdown(parsed)["highest_degree"] == "MASTER"


def test_school_and_degree_both_push_down() -> None:
    """「985 本科及以上」两条 MUST 同时下推：学校档次 + 学历。"""
    parsed = {"exact_constraints": [
        _constraint("school_level", ["985"]),
        _constraint("degree", ["本科"]),
    ]}
    pushed = _hard_filter_pushdown(parsed)
    assert pushed["school_level"] == "985"
    assert pushed["highest_degree"] == "BACHELOR"


def test_industry_must_pushes_down_to_body_terms() -> None:
    parsed = {"exact_constraints": [_constraint("industry", ["支付"])]}
    assert _hard_filter_pushdown(parsed)["evidence_terms"] == ("支付",)


def test_soft_only_kinds_never_push_down_even_if_stored_as_must() -> None:
    """存量库里可能还留着加固前写入的 skill / other_keyword MUST，读侧同样不认。

    否则历史脏数据仍会把候选集清空——这正是「必须 Google Vertex AI SDK」导致全库归零的成因。
    """
    parsed = {"exact_constraints": [
        _constraint("skill", ["Google Vertex AI SDK"]),
        _constraint("other_keyword", ["5年以上"]),
    ]}
    assert _hard_filter_pushdown(parsed) == {}
    assert _constraint_summary(parsed) == ()


def test_non_must_and_unknown_kinds_are_not_pushed_down() -> None:
    parsed = {"exact_constraints": [
        _constraint("company_history", ["字节"], strength="PLUS"),
        _constraint("company_history", ["阿里"], source_text=""),
        _constraint("no_such_kind", ["x"]),
    ]}
    assert _hard_filter_pushdown(parsed) == {}


def test_constraint_summary_exposes_kind_alternatives_and_source() -> None:
    """匹配结果页要能明示「为什么这些人被排除」，故摘要必须带原文依据。"""
    parsed = {"exact_constraints": [
        _constraint("company_history", ["字节", "阿里"], source_text="只要字节或阿里背景"),
        _constraint("company_history", ["腾讯"], strength="PLUS"),
    ]}
    assert _constraint_summary(parsed) == ({
        "kind": "company_history",
        "alternatives": ["字节", "阿里"],
        "source_text": "只要字节或阿里背景",
    },)


def test_empty_or_malformed_parsed_data_is_safe() -> None:
    assert _hard_filter_pushdown({}) == {}
    assert _constraint_summary({}) == ()
    assert _hard_filter_pushdown({"exact_constraints": None}) == {}
    assert _hard_filter_pushdown({"exact_constraints": ["not-a-dict", 42]}) == {}
