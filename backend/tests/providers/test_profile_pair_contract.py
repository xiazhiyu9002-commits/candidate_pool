"""画像双形态结构化契约测试：一致性校验 + 确定性回退。"""
from __future__ import annotations

from kerui_recruit.providers.profile_pair import ProfilePair, ProfileFact, ProfilePoint, build_profile_pair, reconcile_pair, split_profile_clauses


def test_build_profile_pair_splits_lines_and_derives_compact():
    pair = build_profile_pair("7年后端研发\n主导交易系统\n精通 Java 微服务")
    assert pair.narrative.startswith("7年后端研发")
    assert [p.text for p in pair.points] == ["7年后端研发", "主导交易系统", "精通 Java 微服务"]
    assert pair.compact == "7年后端研发"


def test_split_profile_clauses_breaks_single_paragraph_into_points():
    # 单段人工画像也应拆成真正的多分点，且不新增事实。
    points = split_profile_clauses("7年后端经验，主导交易系统，精通Java微服务；负责团队管理")
    assert points == ["7年后端经验", "主导交易系统", "精通Java微服务", "负责团队管理"]
    # 枚举顿号不切分。
    assert split_profile_clauses("精通 Java、Python、Go") == ["精通 Java、Python、Go"]


def test_consistent_pair_requires_facts_in_both_forms():
    pair = ProfilePair(
        facts=[ProfileFact(text="交易系统", evidence_paths=["projects[0].summary"])],
        narrative="负责交易系统后端研发",
        points=[ProfilePoint(text="负责交易系统后端研发")],
        compact="后端研发",
    )
    assert pair.is_consistent() is True


def test_fact_only_in_one_form_is_inconsistent():
    pair = ProfilePair(
        facts=[ProfileFact(text="交易系统", evidence_paths=["projects[0].summary"])],
        narrative="负责交易系统后端研发",
        points=[ProfilePoint(text="负责支付系统后端研发")],
        compact="后端研发",
    )
    assert pair.is_consistent() is False


def test_reconcile_pair_rebuilds_narrative_from_points_when_facts_are_rewritten():
    # 模型把 facts 写成 points 的改写：整体段落未逐字包含全部分点，按分点重建后应自洽。
    pair = ProfilePair(
        facts=[ProfileFact(text="张三具备5年后端研发经验的工程师")],
        narrative="张三具备5年后端研发经验的工程师",
        points=[
            ProfilePoint(text="5年后端研发经验", evidence_paths=["experiences[0].summary"]),
            ProfilePoint(text="负责交易系统"),
        ],
        compact="5年后端研发",
    )
    reconciled = reconcile_pair(pair)
    assert reconciled is not pair
    # 整体段落由分点顺序拼成，且必须是「整段无换行」。
    assert reconciled.narrative == "5年后端研发经验。负责交易系统"
    assert "\n" not in reconciled.narrative
    assert reconciled.is_consistent() is True
    assert reconciled.facts[0].evidence_paths == ["experiences[0].summary"]


def test_reconcile_pair_keeps_narrative_containing_all_points():
    # facts 是改写（未逐字命中分点），但整体段落已包含全部分点：保留模型原文段落。
    pair = ProfilePair(
        facts=[ProfileFact(text="主导交易系统重构，并负责交易系统后端研发")],
        narrative="负责交易系统后端研发，主导交易系统重构",
        points=[ProfilePoint(text="负责交易系统后端研发"), ProfilePoint(text="主导交易系统重构")],
        compact="后端研发",
    )
    reconciled = reconcile_pair(pair)
    assert reconciled is not pair
    assert reconciled.narrative == pair.narrative
    assert [f.text for f in reconciled.facts] == ["负责交易系统后端研发", "主导交易系统重构"]
    assert reconciled.is_consistent() is True


def test_narrative_and_points_are_normalized_to_single_line():
    # 形态不变量：narrative 恒为整段无换行；分点自身也不得带换行。
    pair = ProfilePair(
        narrative="7年后端研发\n主导交易系统",
        points=[ProfilePoint(text="负责支付系统\n主导重构")],
    )
    assert pair.narrative == "7年后端研发。主导交易系统"
    assert pair.points[0].text == "负责支付系统。主导重构"


def test_reconcile_pair_falls_back_to_clause_split_without_points():
    pair = ProfilePair(narrative="7年后端研发，主导交易系统")
    reconciled = reconcile_pair(pair)
    assert [p.text for p in reconciled.points] == ["7年后端研发", "主导交易系统"]
    assert reconciled.is_consistent() is True
