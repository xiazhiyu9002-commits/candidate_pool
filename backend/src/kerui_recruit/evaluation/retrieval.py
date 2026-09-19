"""检索与匹配评测的指标函数。

只依赖标注 ID 和排序结果，不读取真实简历、数据库或索引。所有指标对
排序结果先去重（重复候选人 ID 只计一次）；无相关标签的样本不并入平均值。
"""
from __future__ import annotations

import math


def _dedupe(ranked_ids) -> list:
    return list(dict.fromkeys(ranked_ids))


def recall_at_k(ranked_ids, relevant_ids, k) -> float | None:
    """前 k 个去重结果中相关文档数 / 总相关文档数；无相关标签返回 None。"""
    relevant = set(relevant_ids)
    if not relevant:
        return None
    top = _dedupe(ranked_ids)[:k]
    return sum(1 for x in top if x in relevant) / len(relevant)


def precision_at_k(ranked_ids, relevant_ids, k) -> float | None:
    """前 k 个去重结果中相关文档数 / 实际返回数；无相关标签或 k<=0 返回 None。"""
    relevant = set(relevant_ids)
    if not relevant or k <= 0:
        return None
    top = _dedupe(ranked_ids)[:k]
    if not top:
        return None
    return sum(1 for x in top if x in relevant) / len(top)


def ndcg_at_k(ranked_ids, relevant_scores, k) -> float | None:
    """nDCG@k：等级相关（值越大越相关）；无正相关标签返回 None。"""
    top = _dedupe(ranked_ids)[:k]
    gains = [relevant_scores.get(x, 0) for x in top]
    if not any(g > 0 for g in gains):
        return None
    dcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted((g for g in relevant_scores.values() if g > 0), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return None
    return dcg / idcg


def mrr_at_k(ranked_ids, relevant_scores, k, threshold: int = 2) -> float:
    """MRR@k 的单样本分量：首个等级相关文档排名倒数；k 内无相关返回 0.0。"""
    for rank, doc_id in enumerate(_dedupe(ranked_ids)[:k], start=1):
        if relevant_scores.get(doc_id, 0) >= threshold:
            return 1.0 / rank
    return 0.0


def coverage_at_k(cases, ranked_ids, k, threshold: int = 2) -> float | None:
    """Coverage@k：至少召回 1 个相关候选人的查询占比；无任何相关查询返回 None。"""
    covered = 0
    total = 0
    for case in cases:
        relevant = {
            doc_id for doc_id, grade in (case.get("relevant") or {}).items()
            if grade >= threshold
        }
        if not relevant:
            continue
        total += 1
        if relevant.intersection(_dedupe(ranked_ids.get(case["id"], []))[:k]):
            covered += 1
    return covered / total if total else None


def _average(values) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def evaluate_search(cases, ranked_ids) -> dict:
    """对搜索标注集计算检索指标。

    cases: [{"id": str, "relevant": {doc_id: grade}}]
    ranked_ids: {case_id: [doc_id, ...]}
    返回 recall_50 / recall_100 / precision_10 / ndcg_10 的平均值。
    """
    recall_50, recall_100, precision_10, ndcg_10 = [], [], [], []
    for case in cases:
        case_id = case["id"]
        relevant = case.get("relevant") or {}
        if not relevant:
            continue
        ranked = ranked_ids.get(case_id, [])
        recall_50.append(recall_at_k(ranked, set(relevant), 50))
        recall_100.append(recall_at_k(ranked, set(relevant), 100))
        precision_10.append(precision_at_k(ranked, set(relevant), 10))
        ndcg_10.append(ndcg_at_k(ranked, relevant, 10))
    return {
        "recall_50": _average(recall_50),
        "recall_100": _average(recall_100),
        "precision_10": _average(precision_10),
        "ndcg_10": _average(ndcg_10),
    }


def evaluate_match(pairs, decisions) -> dict:
    """对匹配标注集计算精度与硬条件违规数。

    pairs: [{"id": str, "label": "relevant"|"irrelevant"|"hard_violation"}]
    decisions: {pair_id: "recommend"|"pending"|"reject"}
    precision@k 只统计 decision=recommend 的前 k 个配对中 label=relevant 的比例；
    hard_violation_count 统计「硬条件违规却仍被 recommend」的数量。
    """
    labels = {p["id"]: p["label"] for p in pairs}
    recommended = [p["id"] for p in pairs if decisions.get(p["id"]) == "recommend"]

    def precision(k: int) -> float | None:
        top = recommended[:k]
        if not top:
            return None
        return sum(1 for pid in top if labels.get(pid) == "relevant") / len(top)

    hard_violation_count = sum(
        1 for p in pairs
        if decisions.get(p["id"]) == "recommend" and p["label"] == "hard_violation"
    )
    return {
        "precision_5": precision(5),
        "precision_10": precision(10),
        "hard_violation_count": hard_violation_count,
    }
