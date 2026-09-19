"""真实查询集的检索指标（keyword / vector / hybrid 及其 *_parent 对照变体）。

输入：``.tmp-real-eval/real_labels.json``（确定性弱监督 grade 0–3）、
     ``.tmp-real-eval/label_stats.json``（分桶/口径元数据）、
     ``.tmp-real-eval/retrieval/real_<mode>.json``（别名 ID 排序）。
输出：``.tmp-real-eval/summary.json`` + 控制台对比表。

变体：``keyword`` / ``vector`` / ``hybrid`` 为生产行为；``vector_parent`` /
``hybrid_parent`` 把向量通道限定 ``chunk_type='parent'``，用于量出「child 进入
候选池」的净效果（见 run_real_eval_2026_09_18.py 的同名变体）。

口径（**必须与上限同读**，否则结论会反向）：
- 真实查询的相关集远大于 K（grade>=1 中位数 318/1515），因此 `recall@K` 被
  ``min(1, K/n_relevant)`` 的天花板压制；同时报告 raw / cap / norm=raw/cap。
- grade 门槛同时给 >=1 / >=2 / >=3 三档；>=3 有 38/99 查询为空，指标按「有相关项
  的查询」取均值并报告 n。
- NDCG@10 用等级增益（2^grade-1），不受相关集大小影响，是跨查询最稳的横向指标。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.evaluation.retrieval import (  # noqa: E402
    coverage_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)

EVAL_DIR = ROOT / ".tmp-real-eval"
# 带 _parent 后缀的变体把向量通道限定 chunk_type='parent'，用于隔离 child 入池的净效果
MODES = ("keyword", "vector", "hybrid", "vector_parent", "hybrid_parent")
K_RECALL, K_RANK, K_PRECISION, K_MRR = 20, 10, 5, 20


def _mean(values):
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def _norm(value, cap):
    if value is None or not cap:
        return None
    return round(value / cap, 4)


def load_cases() -> tuple[list[dict], dict]:
    queries = json.loads((EVAL_DIR / "real_queries.json").read_text(encoding="utf-8"))
    labels = json.loads((EVAL_DIR / "real_labels.json").read_text(encoding="utf-8"))
    stats = json.loads((EVAL_DIR / "label_stats.json").read_text(encoding="utf-8"))["labeled"]
    cases = []
    for query in queries:
        qid = query["qid"]
        relevant = {cid: int(grade) for cid, grade in (labels.get(qid) or {}).items()}
        cases.append({"id": qid, "relevant": relevant, "buckets": tuple(query["buckets"]),
                      "title": query.get("title") or "", "runs": query.get("runs", 0)})
    return cases, stats


def load_ranked() -> dict[str, dict[str, list[str]]]:
    ranked: dict[str, dict[str, list[str]]] = {}
    for mode in MODES:
        path = EVAL_DIR / "retrieval" / f"real_{mode}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        ranked[mode] = {qid: list(item.get(mode) or []) for qid, item in data.items()}
    return ranked


def golden(case: dict, threshold: int) -> set[str]:
    return {cid for cid, grade in case["relevant"].items() if grade >= threshold}


def compute(cases: list[dict], ranked: dict[str, list[str]]) -> dict:
    ids = {c["id"] for c in cases}
    ranked_sub = {c["id"]: ranked.get(c["id"], []) for c in cases}
    relevant = {c["id"]: golden(c, 1) for c in cases}
    caps = {k: _mean([min(1.0, k / len(relevant[c["id"]])) for c in cases if relevant[c["id"]]])
            for k in (10, 20, 50, 100)}
    row = {
        "queries": len(cases),
        # 召回（grade>=1）：raw 与 cap 必须同读
        "recall10": _mean([recall_at_k(ranked_sub[c["id"]], relevant[c["id"]], 10) for c in cases]),
        "recall20": _mean([recall_at_k(ranked_sub[c["id"]], relevant[c["id"]], 20) for c in cases]),
        "recall50": _mean([recall_at_k(ranked_sub[c["id"]], relevant[c["id"]], 50) for c in cases]),
        "recall100": _mean([recall_at_k(ranked_sub[c["id"]], relevant[c["id"]], 100) for c in cases]),
        "recall20_cap": caps[20],
        "recall100_cap": caps[100],
        # 分等级门槛召回（K=20）：看清「宽相关」与「强相关」的差距
        "recall20_g2": _mean([recall_at_k(ranked_sub[c["id"]], golden(c, 2), 20) for c in cases]),
        "recall20_g3": _mean([recall_at_k(ranked_sub[c["id"]], golden(c, 3), 20) for c in cases]),
        "recall100_g2": _mean([recall_at_k(ranked_sub[c["id"]], golden(c, 2), 100) for c in cases]),
        "recall100_g3": _mean([recall_at_k(ranked_sub[c["id"]], golden(c, 3), 100) for c in cases]),
        # 排序质量：NDCG 用等级增益，不受相关集大小影响
        "ndcg10": _mean([ndcg_at_k(ranked_sub[c["id"]], c["relevant"], K_RANK) for c in cases]),
        "mrr20": _mean([mrr_at_k(ranked_sub[c["id"]], c["relevant"], K_MRR) for c in cases]),
        "p5_g2": _mean([
            (sum(1 for cid in ranked_sub[c["id"]][:K_PRECISION] if c["relevant"].get(cid, 0) >= 2) / K_PRECISION)
            if ranked_sub[c["id"]] else None for c in cases]),
        "p10_g3": _mean([
            (sum(1 for cid in ranked_sub[c["id"]][:10] if c["relevant"].get(cid, 0) >= 3) / 10)
            if ranked_sub[c["id"]] else None for c in cases]),
        "coverage20_g2": coverage_at_k(cases, ranked_sub, 20, threshold=2),
        # 逐查询诊断
        "empty": sum(1 for c in cases if not ranked_sub[c["id"]]),
        "rg3_n": sum(1 for c in cases if golden(c, 3)),
        "rg3_cap": _mean([min(1.0, 20 / len(golden(c, 3))) for c in cases if golden(c, 3)]),
    }
    row["recall20_norm"] = _norm(row["recall20"], row["recall20_cap"])
    row["recall100_norm"] = _norm(row["recall100"], row["recall100_cap"])
    return row


def main() -> None:
    cases, label_stats = load_cases()
    ranked = load_ranked()
    if not ranked:
        raise SystemExit("未找到检索结果，先运行 run_real_eval_2026_09_18.py")

    subsets = {
        "all": tuple(c["id"] for c in cases),
        "hard": tuple(c["id"] for c in cases if "hard" in c["buckets"]),
        "mgmt": tuple(c["id"] for c in cases if "mgmt" in c["buckets"]),
        "plain": tuple(c["id"] for c in cases if "plain" in c["buckets"]),
    }
    summary = {
        "definition": (
            "real queries from match_run.query_text with jd_revision.parsed_data; labels = "
            "coverage of effective required_skills entries, grade 3/2/1 at coverage >=1.0/0.6/0.4; "
            "no LLM in the loop; retrieval unfiltered (CandidateFilters empty), limit=100"
        ),
        "label_stats": label_stats,
        "universe": json.loads((EVAL_DIR / "label_stats.json").read_text(encoding="utf-8"))["universe"],
        "variants": {},
    }
    for mode, per_query in ranked.items():
        summary["variants"][mode] = {
            name: compute([c for c in cases if c["id"] in subset], per_query)
            for name, subset in subsets.items() if subset
        }

    (EVAL_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")

    keys = ("recall20", "recall20_cap", "recall20_norm", "recall100", "recall20_g2",
            "recall20_g3", "ndcg10", "mrr20", "p5_g2", "p10_g3", "coverage20_g2")
    width = 15
    header = f"{'variant':<15}" + "".join(f"{k:>{width}}" for k in keys)
    print(header)
    print("-" * len(header))
    for mode, groups in summary["variants"].items():
        row = groups["all"]
        print(f"{mode:<15}" + "".join(
            f"{'n/a':>{width}}" if row.get(k) is None else f"{row[k]:>{width}.4f}" for k in keys))
    print("\nall-subset 说明：recall20_raw 被相关集规模压制，读 recall20_norm = raw/cap；"
          "ndcg10 为等级增益，跨查询可比。")

    print("\n分桶 recall20（raw / norm）与 n:")
    for mode, groups in summary["variants"].items():
        parts = []
        for name in ("all", "hard", "mgmt", "plain"):
            row = groups.get(name)
            if not row:
                continue
            parts.append(f"{name}(n={row['queries']})={row['recall20']}/{row['recall20_norm']}")
        print(f"{mode:<15} " + "  ".join(parts))

    print("\n排序层（NDCG@10 / MRR@20 / P@5 grade>=2 / P@10 grade==3 / grade>=3 覆盖查询数）:")
    for mode, groups in summary["variants"].items():
        row = groups["all"]
        print(f"{mode:<15} ndcg10={row['ndcg10']} mrr20={row['mrr20']} p5_g2={row['p5_g2']} "
              f"p10_g3={row['p10_g3']} rg3_n={row['rg3_n']}(cap={row['rg3_cap']})")


if __name__ == "__main__":
    main()
