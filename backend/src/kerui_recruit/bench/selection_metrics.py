"""十种选人方式量化验收的指标计算（纯函数，可单测）。

按《十种选人方式量化验收与 Trae 执行要求》第 3.2 节实现三个指标：

- ``p_adapt_at_k``：自适应前列有效率 P_adapt@K（检索 K=10，双向匹配 K=5）
- ``key_result_recall``：关键结果找回率（前 20）
- ``ndcg_at_k``：排序质量 nDCG@10（增益 2^标签-1，折扣 log2(排名+1)）

指标逐查询计算，再对每种方式分别取平均；本模块只提供单查询计算，聚合由调用方完成。
"""
from __future__ import annotations

import math


def p_adapt_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """自适应前列有效率 P_adapt@K。

    R = 冻结评审范围内所有已标注有效结果的数量（= len(relevant_ids)）
    n = min(K, R)
    分数 = 实际排名前 n 个位置中有效结果数 / n；缺失位置按 0 计。

    当 R=0 时返回 0.0（此类查询应进入单独的无结果测试，不进入正例均值）。
    """
    r = len(relevant_ids)
    n = min(k, r)
    if n == 0:
        return 0.0
    hits = sum(1 for cid in ranked_ids[:n] if cid in relevant_ids)
    return hits / n


def key_result_recall(ranked_ids: list[str], key_ids: set[str], top: int = 20) -> float:
    """关键结果找回率：每个正例查询预先确定的 1~5 个关键结果进入前 top 条的比例。

    无关键结果时返回 0.0（不应进入正例均值）。
    """
    if not key_ids:
        return 0.0
    top_ids = set(ranked_ids[:top])
    return len(key_ids & top_ids) / len(key_ids)


def ndcg_at_k(ranked_ids: list[str], gains: dict[str, int], k: int = 10) -> float:
    """nDCG@K 排序质量。

    gains: entity_id -> 0..3 级标签。增益 = 2^标签 - 1；折扣 = log2(rank+1)。
    缺失位置增益为 0。理想排序来自同一冻结评审范围（gains 中所有标注对象的理想增益降序）。
    理想增益为 0 的查询（idcg==0）返回 0.0，由调用方单列，不记满分。
    """
    dcg = 0.0
    for rank, cid in enumerate(ranked_ids[:k], start=1):
        gain = 2 ** gains.get(cid, 0) - 1
        dcg += gain / math.log2(rank + 1)

    ideal = sorted((2 ** label - 1 for label in gains.values()), reverse=True)
    idcg = 0.0
    for rank, gain in enumerate(ideal[:k], start=1):
        idcg += gain / math.log2(rank + 1)

    if idcg == 0.0:
        return 0.0
    return dcg / idcg
