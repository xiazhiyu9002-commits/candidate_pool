from __future__ import annotations

from kerui_recruit.bench.selection_metrics import (
    key_result_recall,
    ndcg_at_k,
    p_adapt_at_k,
)


def test_p_adapt_returns_ratio_over_min_k_relevant():
    # 8 个合适结果、K=5，前 5 只有 1 个命中 => 1/5 = 0.2（合同示例）。
    ranked = ["a", "b", "c", "d", "e"]
    relevant = {"a", "f", "g", "h", "i", "j", "k", "l"}
    assert p_adapt_at_k(ranked, relevant, k=5) == 0.2


def test_p_adapt_small_relevant_pool_full_hit_scores_one():
    # 库里只有 2 个合适结果、K=5，前 2 都找到 => 2/2 = 1.0（合同示例）。
    ranked = ["a", "b", "c", "d", "e"]
    relevant = {"a", "b"}
    assert p_adapt_at_k(ranked, relevant, k=5) == 1.0


def test_p_adapt_zero_relevant_is_zero():
    assert p_adapt_at_k(["a"], set(), k=10) == 0.0


def test_key_result_recall_counts_only_key_ids():
    ranked = ["x", "a", "y", "b"]
    assert key_result_recall(ranked, {"a", "b", "c"}, top=20) == 2 / 3
    assert key_result_recall(ranked, set(), top=20) == 0.0


def test_ndcg_rewards_better_rankings():
    # 理想：label 3 在最前。把 label 3 放第 1 名的 nDCG 应高于放第 2 名。
    gains = {"a": 3, "b": 1}
    better = ndcg_at_k(["a", "b"], gains, k=10)
    worse = ndcg_at_k(["b", "a"], gains, k=10)
    assert better > worse


def test_ndcg_zero_ideal_gain_is_zero():
    assert ndcg_at_k(["a"], {"a": 0}, k=10) == 0.0
