"""指标函数单元测试：检索/匹配评测的可复算指标。

只覆盖指标计算本身，不加载真实简历、数据库或索引。
"""
from __future__ import annotations

import pytest

from kerui_recruit.evaluation.retrieval import (
    evaluate_match,
    evaluate_search,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_recall_precision_basic():
    # 排序 [A,B,C]，相关 {A,C}，k=2：命中 {A}，recall=1/2，precision=1/2
    ranked = ["A", "B", "C"]
    relevant = {"A", "C"}
    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert precision_at_k(ranked, relevant, 2) == 0.5


def test_deduplicate_ranked_ids():
    # 重复 ID 只计一次：去重后 [A,B,C]
    ranked = ["A", "A", "B", "C"]
    relevant = {"A", "C"}
    assert recall_at_k(ranked, relevant, 3) == 1.0
    assert precision_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)


def test_skip_empty_relevance_in_evaluate_search():
    cases = [
        {"id": "q1", "relevant": {"A": 3, "C": 1}},
        {"id": "q2", "relevant": {}},  # 无相关标签，不并入平均值
    ]
    ranked_ids = {"q1": ["A", "B", "C"], "q2": ["X"]}
    result = evaluate_search(cases, ranked_ids)
    # 只统计 q1：相关 {A,C}，ranked [A,B,C]
    # recall@50 = 2/2 = 1.0，precision@10 = 2/3，ndcg@10 仅由 q1 决定
    assert result["recall_50"] == 1.0
    assert result["precision_10"] == pytest.approx(2 / 3)


def test_evaluate_search_returns_required_keys():
    result = evaluate_search(
        [{"id": "q1", "relevant": {"A": 3}}],
        {"q1": ["A", "B"]},
    )
    assert set(result.keys()) == {"recall_50", "recall_100", "precision_10", "ndcg_10"}


def test_evaluate_match_returns_required_keys_and_counts_violations():
    pairs = [
        {"id": "p1", "label": "relevant"},
        {"id": "p2", "label": "hard_violation"},
        {"id": "p3", "label": "irrelevant"},
    ]
    decisions = {"p1": "recommend", "p2": "recommend", "p3": "reject"}
    result = evaluate_match(pairs, decisions)
    assert set(result.keys()) == {"precision_5", "precision_10", "hard_violation_count"}
    # p2 是硬条件违规却被 recommend，计数 1
    assert result["hard_violation_count"] == 1


def test_ndcg_prefers_higher_relevant_first():
    relevant_scores = {"A": 3, "B": 1}
    # 最优顺序 [A, B] 的 DCG 高于 [B, A]
    assert ndcg_at_k(["A", "B"], relevant_scores, 2) == pytest.approx(1.0)
    assert ndcg_at_k(["B", "A"], relevant_scores, 2) < 1.0
