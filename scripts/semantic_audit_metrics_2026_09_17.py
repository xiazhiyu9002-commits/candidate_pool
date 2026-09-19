"""Compute pool-limited search quality metrics from blind 0-3 judgments."""
from __future__ import annotations

import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"


def dcg(grades: list[int]) -> float:
    return sum((2 ** grade - 1) / math.log2(index + 2)
               for index, grade in enumerate(grades))


def main() -> None:
    retrieval = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    labels = json.loads((SNAPSHOT / "blind_labels.json").read_text(encoding="utf-8"))
    per_query = {}
    for qid, item in retrieval.items():
        if qid > "Q24":
            continue
        pool = set().union(*(set(mode["ids"]) for mode in item["modes"].values()))
        judged = labels.get(qid, {})
        missing = pool - set(judged)
        if missing:
            per_query[qid] = {"missing_labels": len(missing), "pool_size": len(pool)}
            continue
        grades = {cid: int(judged[cid]["grade"]) for cid in pool}
        relevant = {cid for cid, grade in grades.items() if grade >= 2}
        ideal = sorted(grades.values(), reverse=True)[:10]
        ideal_dcg = dcg(ideal)
        mode_metrics = {}
        for mode, result in item["modes"].items():
            ids = result["ids"]
            top_grades = [grades[cid] for cid in ids[:10]]
            mode_metrics[mode] = {
                "returned": len(ids),
                "p5": sum(grade >= 2 for grade in top_grades[:5]) / 5,
                "p10": sum(grade >= 2 for grade in top_grades) / 10,
                "p10_grade3": sum(grade == 3 for grade in top_grades) / 10,
                "ndcg10": dcg(top_grades) / ideal_dcg if ideal_dcg else None,
                "recall50_pool": len(relevant.intersection(ids[:50])) / len(relevant) if relevant else None,
                "recall100_pool": len(relevant.intersection(ids[:100])) / len(relevant) if relevant else None,
                "top10_grades": top_grades,
            }
        per_query[qid] = {"intent": item["intent"], "pool_size": len(pool),
                          "pool_relevant": len(relevant), "modes": mode_metrics}
    macro = {}
    for mode in ("keyword", "vector", "hybrid"):
        rows = [q["modes"][mode] for q in per_query.values() if "modes" in q]
        macro[mode] = {key: round(statistics.mean(row[key] for row in rows if row[key] is not None), 4)
                       if any(row[key] is not None for row in rows) else None
                       for key in ("p5", "p10", "p10_grade3", "ndcg10", "recall50_pool", "recall100_pool")}
        macro[mode]["queries"] = len(rows)
    output = {"definition": "grades >=2 relevant; nDCG gain=2^grade-1; recall denominator=union of all modes top100",
              "macro": macro, "per_query": per_query}
    (SNAPSHOT / "search_metrics.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(macro, ensure_ascii=False))
    print("fully_labeled", sum("modes" in q for q in per_query.values()), "/", len(per_query))


if __name__ == "__main__":
    main()
