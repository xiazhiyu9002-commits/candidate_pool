"""可重复的业务评测脚本（v3）：搜索 / 双向匹配 / 职业方向三部分基线。

数据源为 ``.semantic-audit-snapshot`` 下的冻结 JSON（只读），与固定索引/数据库副本对应。
``--baseline`` 会对照 2026-09-17 语义盲评报告的基线数值，输出逐项 PASS/FAIL。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / ".semantic-audit-snapshot"

# 报告基线（小样本模型评测，非真实猎头满意率；仅用于确认数据/索引版本未漂移）。
BASELINE = {
    "keyword_p5": 0.717,
    "vector_p5": 0.375,
    "hybrid_p5": 0.8583,
    "vector_empty_queries": 6,
    "forward_returned_pairs": 14,
    "reverse_returned_pairs": 5,
    "direction_consistent": 40,
    "direction_total": 52,
}


def _read(snapshot: Path, name: str) -> dict:
    return json.loads((snapshot / name).read_text(encoding="utf-8"))


def dcg(grades: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def p_at_k(grades: list[int], k: int) -> float:
    """相关等级 >=2 计为相关；P@k 分母固定 k，空位按 0。"""
    return sum(1 for g in grades[:k] if g >= 2) / k


def evaluate_search(snapshot: Path) -> dict:
    retrieval = _read(snapshot, "retrieval.json")["intents"]
    labels = _read(snapshot, "blind_labels.json")
    per_query: dict[str, dict] = {}
    empty = {"keyword": 0, "vector": 0, "hybrid": 0}
    for qid, item in retrieval.items():
        if qid > "Q24":
            continue
        pool = set().union(*(set(m["ids"]) for m in item["modes"].values()))
        judged = labels.get(qid, {})
        if pool - set(judged):
            per_query[qid] = {"fully_labeled": False, "pool_size": len(pool)}
            continue
        grades = {cid: int(judged[cid]["grade"]) for cid in pool}
        ideal = sorted(grades.values(), reverse=True)[:10]
        ideal_dcg = dcg(ideal)
        modes = {}
        for mode, result in item["modes"].items():
            ids = result["ids"]
            top = [grades[cid] for cid in ids[:10]]
            if not ids:
                empty[mode] += 1
            modes[mode] = {
                "returned": len(ids),
                "p5": p_at_k(top, 5),
                "p10": p_at_k(top, 10),
                "ndcg10": round(dcg(top) / ideal_dcg, 4) if ideal_dcg else None,
            }
        per_query[qid] = {"intent": item["intent"], "fully_labeled": True,
                          "pool_size": len(pool), "modes": modes}
    macro = {}
    for mode in ("keyword", "vector", "hybrid"):
        rows = [q["modes"][mode] for q in per_query.values() if q.get("fully_labeled") and mode in q.get("modes", {})]
        macro[mode] = {
            key: round(statistics.mean(r[key] for r in rows if r[key] is not None), 4)
            if any(r[key] is not None for r in rows) else None
            for key in ("p5", "p10", "ndcg10")
        }
        macro[mode]["queries"] = len(rows)
    return {"macro": macro, "empty_queries": empty, "per_query": per_query}


def evaluate_match(snapshot: Path) -> dict:
    results = _read(snapshot, "match_results.json")
    labels = _read(snapshot, "match_blind_labels.json")["labels"]

    def count_pairs(section: dict, key: str) -> dict:
        total = 0
        per_entity = {}
        for entity, modes in section.items():
            items = modes.get("hybrid", {}).get(key, [])
            total += len(items)
            per_entity[entity] = len(items)
        return {"total_returned": total, "per_entity": per_entity}

    # 正向（JD 找人）返回候选人 id，反向（人找 JD）返回岗位 jd id。
    forward = count_pairs(results.get("forward", {}), "ids")
    reverse = count_pairs(results.get("reverse", {}), "jds")

    verdict_dist = {"recommend": 0, "pending": 0, "reject": 0}
    grade_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    for pair, label in labels.items():
        verdict_dist[label.get("verdict", "pending")] = verdict_dist.get(label.get("verdict", "pending"), 0) + 1
        grade_dist[int(label.get("grade", 0))] = grade_dist.get(int(label.get("grade", 0)), 0) + 1

    return {
        "forward_returned": forward["total_returned"],
        "reverse_returned": reverse["total_returned"],
        "forward_per_entity": forward["per_entity"],
        "reverse_per_entity": reverse["per_entity"],
        "labeled_pairs": len(labels),
        "verdict_dist": verdict_dist,
        "grade_dist": grade_dist,
    }


def evaluate_direction(snapshot: Path) -> dict:
    cards = _read(snapshot, "direction_blind_cards.json")
    labels = _read(snapshot, "direction_blind_labels.json")["labels"]
    consistent = 0
    diffs = []
    for card in cards:
        cid = card["id"]
        stored = card["stored"]
        model = labels.get(cid, {}).get("direction")
        if model == stored:
            consistent += 1
        else:
            diffs.append({"id": cid, "stored": stored, "model": model,
                          "evidence": labels.get(cid, {}).get("evidence", "")})
    return {"consistent": consistent, "total": len(cards), "diffs": diffs}


def _check(name: str, actual: float | int, expected: float | int, tolerance: float = 0.02) -> bool:
    if isinstance(expected, float):
        return abs(actual - expected) <= tolerance
    return actual == expected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--baseline", action="store_true", help="对照报告基线输出 PASS/FAIL")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    search = evaluate_search(args.snapshot)
    match = evaluate_match(args.snapshot)
    direction = evaluate_direction(args.snapshot)

    report = {"search": search, "match": match, "direction": direction}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print("== 搜索基线（宏平均 P@5 / P@10 / nDCG@10，相关=grade>=2） ==")
    for mode, m in search["macro"].items():
        print(f"  {mode:8s} P@5={m['p5']}  P@10={m['p10']}  nDCG@10={m['ndcg10']}  queries={m['queries']}")
    print(f"  空结果查询数: {search['empty_queries']}")
    print("\n== 匹配基线 ==")
    print(f"  正向（JD找人）hybrid 返回配对: {match['forward_returned']}")
    print(f"  反向（人找JD）hybrid 返回配对: {match['reverse_returned']}")
    print(f"  已标注配对: {match['labeled_pairs']}  判定分布: {match['verdict_dist']}  分级分布: {match['grade_dist']}")
    print("\n== 方向基线 ==")
    print(f"  库内标签与模型一致: {direction['consistent']}/{direction['total']}")

    if args.baseline:
        print("\n== 基线对照 ==")
        checks = [
            ("keyword P@5", search["macro"]["keyword"]["p5"], BASELINE["keyword_p5"]),
            ("vector P@5", search["macro"]["vector"]["p5"], BASELINE["vector_p5"]),
            ("hybrid P@5", search["macro"]["hybrid"]["p5"], BASELINE["hybrid_p5"]),
            ("vector 空结果查询数", search["empty_queries"]["vector"], BASELINE["vector_empty_queries"]),
            ("正向返回配对", match["forward_returned"], BASELINE["forward_returned_pairs"]),
            ("反向返回配对", match["reverse_returned"], BASELINE["reverse_returned_pairs"]),
            ("方向一致数", direction["consistent"], BASELINE["direction_consistent"]),
            ("方向总数", direction["total"], BASELINE["direction_total"]),
        ]
        passed = 0
        for name, actual, expected in checks:
            ok = _check(name, actual, expected)
            passed += int(ok)
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {actual} (期望 {expected})")
        print(f"\n  {passed}/{len(checks)} 项通过")


if __name__ == "__main__":
    main()
