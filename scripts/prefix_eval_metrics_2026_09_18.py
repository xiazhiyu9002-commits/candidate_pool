"""子切片前缀两方案的两层指标计算器。

第一层（召回）：Recall@20、Recall@20 分类（硬条件/软条件）、Coverage@20。
第二层（排序）：NDCG@10、MRR@20、P@5。

输入：统一标签池 + 各变体 top100。
- 标签池 ``.tmp-prefix-eval/blind_labels_ext.json``（缺省回退快照 ``blind_labels.json``）。
- 变体目录 ``.tmp-prefix-eval/retrieval/<name>.json``，结构 ``{qid: {"intent":..., "<mode>": [alias,...]}}``。
输出：``.tmp-prefix-eval/summary.json`` + 打印对比表。

口径：相关判定 grade>=2；召回分母为「扩展池内该 query 的 grade>=2 集合」（pool-limited recall）。
"""
from __future__ import annotations

import json
import statistics
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

EVAL_DIR = ROOT / ".tmp-prefix-eval"
SNAPSHOT = ROOT / ".semantic-audit-snapshot"

# 固定分类：硬条件=带显式约束的意图；软条件=纯方向意图。
QUERY_CLASS = {
    "Q21": "hard", "Q22": "hard", "Q23": "hard",
    **{f"Q{i:02d}": "soft" for i in range(1, 21)},
    "Q24": "soft",
}
LABELED_QUERIES = tuple(sorted(QUERY_CLASS))
THRESHOLDS = {
    "recall20": 0.75, "recall20_hard": 0.90, "recall20_soft": 0.65,
    "coverage20": 0.90, "ndcg10": 0.70, "mrr20": 0.60, "p5": 0.75,
}
K_RECALL, K_RANK, K_PRECISION, K_MRR = 20, 10, 5, 20


def _mean(values):
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def _norm(value, cap):
    """归一化召回：原始召回 / 该池结构下的理论上限。cap 为 0/None 时无意义，返回 None。"""
    if value is None or not cap:
        return None
    return round(value / cap, 4)


def load_labels() -> tuple[dict, str]:
    ext = EVAL_DIR / "blind_labels_ext.json"
    if ext.exists():
        return json.loads(ext.read_text(encoding="utf-8")), "blind_labels_ext.json (model_proxy)"
    return json.loads((SNAPSHOT / "blind_labels.json").read_text(encoding="utf-8")), "blind_labels.json"


def grades_of(labels: dict, qid: str) -> dict[str, int]:
    return {cid: int(item.get("grade", 0)) for cid, item in (labels.get(qid) or {}).items()}


def collect_pool(retrieval_by_variant: dict[str, dict], labels: dict) -> dict[str, list[str]]:
    pool: dict[str, list[str]] = {}
    for qid in LABELED_QUERIES:
        ids: list[str] = []
        seen: set[str] = set()
        for variant in retrieval_by_variant.values():
            for ranked in variant.get(qid, {}).values():
                if not isinstance(ranked, list):
                    continue
                for cid in ranked:
                    if cid not in seen:
                        seen.add(cid)
                        ids.append(cid)
        for cid in labels.get(qid, {}):
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
        pool[qid] = ids
    return pool


def compute(cases: list[dict], ranked: dict[str, list[str]], subset: tuple[str, ...]) -> dict:
    filtered = [c for c in cases if c["id"] in subset]
    ranked_sub = {c["id"]: ranked.get(c["id"], []) for c in filtered}
    # 召回分母只统计 grade>=2 的相关项；`case["relevant"]` 含全部等级，需先过滤。
    golden = {c["id"]: {cid for cid, grade in c["relevant"].items() if grade >= 2} for c in filtered}
    return {
        "recall20": _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL) for c in filtered]),
        "recall20_hard": _mean([
            recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL)
            for c in filtered if QUERY_CLASS[c["id"]] == "hard"
        ]),
        "recall20_soft": _mean([
            recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL)
            for c in filtered if QUERY_CLASS[c["id"]] == "soft"
        ]),
        "coverage20": coverage_at_k(filtered, ranked_sub, K_RECALL),
        "ndcg10": _mean([ndcg_at_k(ranked.get(c["id"], []), c["relevant"], K_RANK) for c in filtered]),
        "mrr20": _mean([mrr_at_k(ranked.get(c["id"], []), c["relevant"], K_MRR) for c in filtered]),
        "p5": _mean([
            (sum(1 for cid in ranked.get(c["id"], [])[:K_PRECISION] if c["relevant"].get(cid, 0) >= 2)
             / K_PRECISION) if ranked.get(c["id"]) else None
            for c in filtered
        ]),
        # 池内 grade>=2 的均值即 Recall@20 的分母；cap 为该池结构下 Recall@20 的理论上限。
        "pool_relevant": _mean([float(len(golden[c["id"]])) for c in filtered]),
        "recall20_cap": _mean([min(1.0, K_RECALL / len(golden[c["id"]])) for c in filtered if golden[c["id"]]]),
        "queries": len(filtered),
        # —— 口径修正：K 与分母错配时，原始 Recall@20 会低估系统；以下为可比口径补充 ——
        # 归一化召回 = 原始召回 / 该池结构下的上限，回答「在上限内拿回了多少」。
        "recall20_norm": _norm(
            _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL) for c in filtered]),
            _mean([min(1.0, K_RECALL / len(golden[c["id"]])) for c in filtered if golden[c["id"]]]),
        ),
        # K=100 参照：本链路 reranker 只重排 hits[:100]，故 R@100 是可比口径下的召回上界参照。
        "recall100": _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], 100) for c in filtered]),
        # 分类指标样本量小，同时给出 n 与 cap，禁止单看数值。
        "hard_n": sum(1 for c in filtered if QUERY_CLASS[c["id"]] == "hard"),
        "recall20_hard_cap": _mean([min(1.0, K_RECALL / len(golden[c["id"]]))
                                    for c in filtered if QUERY_CLASS[c["id"]] == "hard" and golden[c["id"]]]),
        "recall20_soft_cap": _mean([min(1.0, K_RECALL / len(golden[c["id"]]))
                                    for c in filtered if QUERY_CLASS[c["id"]] == "soft" and golden[c["id"]]]),
        # 相关门槛分层：0/1/2/3 三档同时报告，避免单一门槛决定结论。
        "recall20_g1": _mean([recall_at_k(ranked.get(c["id"], []),
                                          {cid for cid, g in c["relevant"].items() if g >= 1}, K_RECALL)
                              for c in filtered]),
        "recall20_g3": _mean([recall_at_k(ranked.get(c["id"], []),
                                          {cid for cid, g in c["relevant"].items() if g >= 3}, K_RECALL)
                              for c in filtered]),
        "recall100_g3": _mean([recall_at_k(ranked.get(c["id"], []),
                                           {cid for cid, g in c["relevant"].items() if g >= 3}, 100)
                               for c in filtered]),
    }


def main() -> None:
    labels, label_source = load_labels()
    retrieval_dir = EVAL_DIR / "retrieval"
    files = sorted(retrieval_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"未找到变体结果：{retrieval_dir}")
    retrieval_by_variant = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in files}

    pool = collect_pool(retrieval_by_variant, labels)
    cases, missing = [], {}
    for qid in LABELED_QUERIES:
        judged = grades_of(labels, qid)
        gap = [cid for cid in pool[qid] if cid not in judged]
        if gap:
            missing[qid] = len(gap)
        cases.append({"id": qid, "relevant": {cid: judged[cid] for cid in pool[qid] if cid in judged}})

    variants: dict[str, dict] = {}
    for name, retrieval in sorted(retrieval_by_variant.items()):
        modes = sorted({key for entry in retrieval.values() for key in entry if key != "intent"})
        for mode in modes:
            ranked = {qid: list(entry.get(mode) or []) for qid, entry in retrieval.items()}
            variants[f"{name}.{mode}"] = {
                **compute(cases, ranked, LABELED_QUERIES),
                "pool_size": _mean([float(len(pool[qid])) for qid in LABELED_QUERIES]),
            }

    summary = {
        "definition": (
            "grades >=2 relevant; recall denominator = pooled (union of all variants' top100 + prior labels) "
            "items with grade>=2 per query, i.e. pool-limited recall; nDCG gain=2^grade-1 over pooled ideal top10; "
            "hard = Q21/Q22/Q23, soft = Q01-Q20+Q24; Q25-Q27 excluded (no blind labels)"
        ),
        "caliber_rules": (
            "pool-limited recall must be read together with recall20_cap (= mean min(1, 20/n_relevant)): "
            "recall20_norm = recall20 / recall20_cap is the share of the attainable ceiling actually retrieved; "
            "recall100 is the comparable-K reference because the hybrid reranker only re-orders hits[:100]; "
            "grade thresholds are reported at >=1/>=2/>=3 so no single threshold decides the verdict; "
            "hard-condition metrics carry hard_n=3, so report n and cap alongside the value"
        ),
        "label_source": label_source,
        "pool_size_total": sum(len(v) for v in pool.values()),
        "missing_labels": missing,
        "thresholds": THRESHOLDS,
        "variants": variants,
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    width = max(len(k) for k in THRESHOLDS) + 4
    header = f"{'variant':<26}" + "".join(f"{k:>{width}}" for k in THRESHOLDS)
    print(header)
    print("-" * len(header))
    for name, row in variants.items():
        line = f"{name:<26}"
        for key, target in THRESHOLDS.items():
            value = row.get(key)
            if value is None:
                line += f"{'n/a':>{width}}"
            else:
                flag = "PASS" if value >= target else "FAIL"
                line += f"{f'{value:.4f} {flag}':>{width}}"
        print(line)
    print(f"\npool_total={summary['pool_size_total']} missing_labels={missing or 'none'}")
    print("target: " + ", ".join(f"{k}>={v}" for k, v in THRESHOLDS.items()))

    # 口径修正表：原始 Recall@20 受 K 与分母错配压制，须与上限/归一化值同读。
    calib_keys = ("recall20", "recall20_cap", "recall20_norm", "recall100",
                  "recall20_g1", "recall20_g3", "recall100_g3", "recall20_hard", "recall20_hard_cap")
    cw = 17
    chead = f"{'variant':<26}" + "".join(f"{k:>{cw}}" for k in calib_keys)
    print("\n" + chead)
    print("-" * len(chead))
    for name, row in variants.items():
        line = f"{name:<26}"
        for key in calib_keys:
            value = row.get(key)
            line += f"{'n/a':>{cw}}" if value is None else f"{value:>{cw}.4f}"
        print(line)
    print("read: recall20_norm = recall20 / recall20_cap（上限内拿回比例）；"
          "recall100 为可比 K 参照（reranker 只重排 hits[:100]）；"
          "hard 类仅 n=3，须与 hard_cap 同读")

    # 分类归一化：把「K/分母错配」的口径损失与「真没召回到」的缺口分开。
    print("\nclass-normalized recall20 (raw / cap):")
    for name, row in variants.items():
        hard = _norm(row.get("recall20_hard"), row.get("recall20_hard_cap"))
        soft = _norm(row.get("recall20_soft"), row.get("recall20_soft_cap"))
        print(f"{name:<26} hard(n={row.get('hard_n')})={hard}  soft={soft}")


if __name__ == "__main__":
    main()
