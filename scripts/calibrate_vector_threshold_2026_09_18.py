"""向量相似度阈值标定：用冻结快照索引的实测分数分布，标定 ``VECTOR_MIN_SIMILARITY``。

背景：``service._apply_vector_threshold`` 用 ``max(0.6, top1*0.9)`` 做门槛，实测在真实
BGE-M3 上 top1 相似度普遍低于 0.6，导致纯向量模式多数查询返回 0 条（15/24）。

本脚本只做测量与仿真，不修改生产代码、不写 ``.dev-data``：
1. 用真实 BGE-M3 嵌入 24 条查询，直接调 ``index.search_vector`` 取原始分数（候选级去重后的排名）；
2. 输出「分数 × 相关等级」的分离度表，据此给出绝对下限的证据；
3. 对多组阈值策略仿真召回结果，用既有盲评标签算两层指标，供决策引用。

产出：``.tmp-prefix-eval/vector_scores.json``、``.tmp-prefix-eval/threshold_calibration.json``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
VARIANTS = ("base", "ai15")
RANK_LIMIT = 300  # 与 service 的 pool_limit（limit=100 时）一致
TOP_K = 100
RELEVANT_GRADE = 2
FLOOR_SWEEP = [round(0.44 + 0.02 * i, 2) for i in range(13)]  # 0.44 ~ 0.68


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mean(values) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


async def embed_queries(emb, queries: dict[str, str]) -> dict[str, list[float]]:
    qids = sorted(queries)
    vectors = await emb.embed_documents([queries[q] for q in qids])
    return dict(zip(qids, vectors))


def collect_scores(indexes: dict, vectors: dict[str, list[float]]) -> dict[str, dict[str, list]]:
    """{variant: {qid: [[alias, score], ...]}}，顺序即向量通道排名（候选级去重）。"""
    from kerui_recruit.search.contracts import CandidateFilters

    filters = CandidateFilters()
    out: dict[str, dict[str, list]] = {}
    for name, index in indexes.items():
        per_query: dict[str, list] = {}
        for qid, vector in vectors.items():
            rows = index.search_vector(tuple(vector), filters, RANK_LIMIT)
            hits = index.hits_from_rows(rows, "vector")
            per_query[qid] = [[alias(hit.candidate_id), round(float(hit.score), 6)] for hit in hits]
        out[name] = per_query
    return out


def grade_stats(scores: dict, labels: dict) -> dict:
    """分数 × 等级分离度：只统计既有标签覆盖到的候选（池内），池外无标签不可用。"""
    buckets: dict[int, list[float]] = {}
    thresholds = [i / 100 for i in range(40, 71)]
    sweep = {t: {0: 0, 1: 0, 2: 0, 3: 0} for t in thresholds}
    for qid, ranked in scores.items():
        grades = {alias_: item["grade"] for alias_, item in (labels.get(qid) or {}).items()}
        for alias_, score in ranked:
            grade = grades.get(alias_)
            if grade is None:
                continue
            buckets.setdefault(grade, []).append(score)
            for t in thresholds:
                if score >= t:
                    sweep[t][grade] += 1

    def quantile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 4)

    summary = {}
    for grade in sorted(buckets):
        values = buckets[grade]
        summary[str(grade)] = {
            "n": len(values),
            "mean": round(_mean(values), 4),
            "p25": quantile(values, 0.25),
            "p50": quantile(values, 0.50),
            "p75": quantile(values, 0.75),
        }

    rows = []
    for t in thresholds:
        kept = sweep[t]
        rel = kept[RELEVANT_GRADE] + kept[3]
        irr = kept[0] + kept[1]
        precision = (rel / (rel + irr)) if (rel + irr) else None
        recall = (rel / (sum(buckets.get(RELEVANT_GRADE, [])) + len(buckets.get(3, [])))) if buckets else None
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
        rows.append({"threshold": round(t, 2), "kept": kept, "kept_relevant": rel, "kept_irrelevant": irr,
                     "precision": precision, "recall": recall, "f1": f1})
    return {"by_grade": summary, "sweep": rows}


def evaluate(scores: dict, labels: dict, threshold) -> dict:
    """给定「top1 -> 阈值」函数，仿真该阈值下的候选列表并算两层指标。"""
    ranked: dict[str, list[str]] = {}
    counts: list[int] = []
    for qid, items in scores.items():
        top = items[0][1] if items else None
        cutoff = threshold(top) if top is not None else None
        kept = [alias_ for alias_, score in items if cutoff is None or score >= cutoff][:TOP_K]
        ranked[qid] = kept
        counts.append(len(kept))

    cases = [{"id": qid, "relevant": {alias_: item["grade"] for alias_, item in items.items()}}
             for qid, items in labels.items()]

    recall20, ndcg10, mrr20, p5 = [], [], [], []
    covered = 0
    for case in cases:
        grades = case["relevant"]
        golden = {a for a, g in grades.items() if g >= RELEVANT_GRADE}
        top20 = ranked.get(case["id"], [])[:20]
        top10 = ranked.get(case["id"], [])[:10]
        top5 = ranked.get(case["id"], [])[:5]
        if golden:
            recall20.append(sum(1 for x in top20 if x in golden) / len(golden))
            covered += 1 if any(x in golden for x in top20) else 0
        if any(grades.get(x, 0) > 0 for x in top10):
            dcg = sum((2 ** grades.get(x, 0) - 1) / __import__("math").log2(i + 2) for i, x in enumerate(top10))
            ideal = sorted((g for g in grades.values() if g > 0), reverse=True)[:10]
            idcg = sum((2 ** g - 1) / __import__("math").log2(i + 2) for i, g in enumerate(ideal))
            ndcg10.append(dcg / idcg if idcg else None)
        first = next((1.0 / rank for rank, x in enumerate(top20, start=1) if grades.get(x, 0) >= RELEVANT_GRADE), 0.0)
        mrr20.append(first)
        if top5:
            p5.append(sum(1 for x in top5 if grades.get(x, 0) >= RELEVANT_GRADE) / len(top5))
    return {
        "mean_returned": round(_mean([float(c) for c in counts]), 2),
        "empty_queries": sum(1 for c in counts if c == 0),
        "recall20": _mean(recall20),
        "coverage20": (covered / len(cases)) if cases else None,
        "ndcg10": _mean(ndcg10),
        "mrr20": _mean(mrr20),
        "p5": _mean(p5),
    }


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    intents = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    queries = {qid: item["intent"] for qid, item in intents.items() if qid <= "Q24"}
    labels = json.loads((EVAL_DIR / "blind_labels_ext.json").read_text(encoding="utf-8"))
    print(f"queries={len(queries)} labeled={sum(len(v) for v in labels.values())}", flush=True)

    scores_path = EVAL_DIR / "vector_scores.json"
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        print("scores: reuse cache", flush=True)
    else:
        async with httpx.AsyncClient(timeout=180) as client:
            emb = SiliconFlowEmbeddingProvider(
                api_key=key, client=client,
                base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
            vectors = await embed_queries(emb, queries)
        indexes = {
            name: LanceDBSearchIndex(EVAL_DIR / "index" / name, vector_dimension=1024,
                                     embedding_model=settings["siliconflow_embedding_model"])
            for name in VARIANTS
        }
        scores = collect_scores(indexes, vectors)
        scores_path.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
        print("scores: written cache", flush=True)

    report: dict = {"top1": {}, "grade_separation": {}, "floor_sweep": {}}
    for name in VARIANTS:
        per_query = scores[name]
        topline = sorted(round(items[0][1], 4) for items in per_query.values() if items)
        report["top1"][name] = {
            "min": topline[0], "median": topline[len(topline) // 2], "max": topline[-1],
            "below_0.6": sum(1 for v in topline if v < 0.6), "n": len(topline),
        }
        print(f"\n== {name} top1: min={topline[0]} median={topline[len(topline)//2]} max={topline[-1]} "
              f"below_0.6={sum(1 for v in topline if v < 0.6)}/{len(topline)}", flush=True)

        separation = grade_stats(per_query, labels)
        report["grade_separation"][name] = separation
        print("   grade  n     mean    p25     p50     p75", flush=True)
        for grade, stat in separation["by_grade"].items():
            print(f"   {grade:<6} {stat['n']:<5} {stat['mean']:<7} {stat['p25']:<7} {stat['p50']:<7} {stat['p75']}", flush=True)
        print("   thr   kept(0/1/2/3)  rel  irr  precision  recall  f1", flush=True)
        for row in separation["sweep"]:
            if round(row["threshold"] * 100) % 2:
                continue
            kept = row["kept"]
            print(f"   {row['threshold']:<5} {kept[0]}/{kept[1]}/{kept[2]}/{kept[3]:<9} "
                  f"{row['kept_relevant']:<4} {row['kept_irrelevant']:<4} "
                  f"{_fmt(row['precision'])} {_fmt(row['recall'])} {_fmt(row['f1'])}", flush=True)

        sweep = {}
        for floor in FLOOR_SWEEP:
            sweep[str(floor)] = evaluate(per_query, labels, lambda top, f=floor: max(f, top * 0.9))
        sweep["relative_only_0.90"] = evaluate(per_query, labels, lambda top: top * 0.9)
        sweep["relative_only_0.80"] = evaluate(per_query, labels, lambda top: top * 0.8)
        sweep["no_threshold"] = evaluate(per_query, labels, lambda top: None)
        sweep["current_0.60"] = evaluate(per_query, labels, lambda top: max(0.60, top * 0.9))
        report["floor_sweep"][name] = sweep

        print("   policy                ret   empty  R@20   cov20  NDCG10  MRR20  P@5", flush=True)
        for label, value in sweep.items():
            print(f"   {label:<20} {value['mean_returned']:<5} {value['empty_queries']:<6} "
                  f"{_fmt(value['recall20'])} {_fmt(value['coverage20'])} {_fmt(value['ndcg10'])} "
                  f"{_fmt(value['mrr20'])} {_fmt(value['p5'])}", flush=True)

    (EVAL_DIR / "threshold_calibration.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwritten: .tmp-prefix-eval/threshold_calibration.json", flush=True)


def _fmt(value) -> str:
    return f"{value:.4f}" if isinstance(value, float) else "-"


if __name__ == "__main__":
    asyncio.run(main())
