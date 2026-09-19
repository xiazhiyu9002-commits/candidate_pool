"""判决式评测第 3 步：按隐藏映射还原，计算分模式指标与失败类型分布。

真值来自子 Agent 依据 **JD 原文** 给出的 0–3 分；检索结果与模式映射在判决期间对判断方不可见。

指标口径：
- ``ndcg@10``  分级增益（gain = 2^grade − 1），理想排序取该 JD 已判决候选池的分数降序；
               各模式共用同一理想值，因此可横向比较。
- ``p@5/10``   强相关（grade ≥ 2）占比。
- ``top1``     第 1 名的分数；``top1_ge2`` 首位是否强相关。
- ``hits@10``  该 JD 已判决候选池中所有强相关者，被本条前 10 命中的比例（同一 JD 分母一致）。
- ``sum@10``   前 10 名分数之和（总量视角）。
- 配对检验：按 (JD, 问法) 逐条比较两个模式的 ndcg@10。
"""
from __future__ import annotations

import collections
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp-judge"
MODES = ("keyword", "vector", "hybrid")
PHRASINGS = ("standard", "colloquial", "vague")
LABEL_CN = {"standard": "标准", "colloquial": "口语化", "vague": "模糊"}
MODE_CN = {"keyword": "关键词", "vector": "向量", "hybrid": "混合"}
FAIL_CN = {
    "wrong_domain": "方向不符",
    "missing_core_skill": "缺核心技术",
    "missing_hard": "缺硬门槛",
    "seniority": "层级不匹配",
    "thin_evidence": "信息不足",
}


def ndcg(seq: list[str], grades: dict[str, int], k: int = 10) -> float | None:
    dcg = sum((2 ** grades.get(c, 0) - 1) / math.log2(i + 2) for i, c in enumerate(seq[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else None


def main() -> None:
    results = json.loads((OUT / "search_results.json").read_text(encoding="utf-8"))
    blinding = json.loads((OUT / "blinding_map.json").read_text(encoding="utf-8"))

    judgements: dict[str, dict[str, dict]] = {}
    gaps = {}
    for n in range(1, 6):
        payload = json.loads((OUT / f"judge_out_batch{n}.json").read_text(encoding="utf-8"))
        for jid, body in payload.items():
            judgements[jid] = {j["label"]: j for j in body["judgements"]}
            gaps[jid] = {"best": body.get("best"), "recall_gap": body.get("recall_gap")}

    # 还原：letter -> alias(候选人) -> grade
    grades: dict[str, dict[str, int]] = {}
    missing = 0
    for jid, info in blinding.items():
        table = judgements.get(jid, {})
        grades[jid] = {}
        for letter, cand in info["letters"].items():
            entry = table.get(letter)
            if entry is None:
                missing += 1
                continue
            grades[jid][cand] = int(entry["grade"])
    print(f"还原完成：JD={len(grades)} 缺判决={missing}")

    # 覆盖性校验：每条检索结果里的候选人都必须有分数
    uncovered = 0
    for jid, lists in results.items():
        for phrasing in PHRASINGS:
            for mode in MODES:
                for cand in lists[phrasing][mode]:
                    if cand not in grades[jid]:
                        uncovered += 1
    print(f"检索结果中无判决分数的条目={uncovered}（应为 0）")
    print()

    per_case: dict[tuple[str, str, str], dict] = {}
    for jid, lists in results.items():
        g = grades[jid]
        for phrasing in PHRASINGS:
            for mode in MODES:
                seq = lists[phrasing][mode]
                strong_pool = [c for c, v in g.items() if v >= 2]
                hit = len([c for c in seq[:10] if g.get(c, 0) >= 2])
                per_case[(jid, phrasing, mode)] = {
                    "ndcg": ndcg(seq, g),
                    "p5": sum(1 for c in seq[:5] if g.get(c, 0) >= 2) / 5,
                    "p10": sum(1 for c in seq[:10] if g.get(c, 0) >= 2) / 10,
                    "top1": g.get(seq[0], 0) if seq else 0,
                    "top1_ge2": 1 if seq and g.get(seq[0], 0) >= 2 else 0,
                    "hits": hit / len(strong_pool) if strong_pool else None,
                    "sum": sum(g.get(c, 0) for c in seq[:10]),
                    "seq": seq,
                }

    def agg(rows: list[dict], key: str) -> float:
        vals = [r[key] for r in rows if r[key] is not None]
        return statistics.fmean(vals) if vals else 0.0

    print("=" * 92)
    print("【一】分模式总览（20 JD × 3 问法 = 60 例）")
    print(f"{'模式':<7}{'ndcg@10':>9}{'P@5':>8}{'P@10':>8}{'首位≥2':>9}{'首位均分':>9}"
          f"{'hits@10':>9}{'sum@10':>8}")
    for mode in MODES:
        rows = [v for k, v in per_case.items() if k[2] == mode]
        print(f"{MODE_CN[mode]:<7}{agg(rows, 'ndcg'):>9.4f}{agg(rows, 'p5'):>8.4f}"
              f"{agg(rows, 'p10'):>8.4f}{agg(rows, 'top1_ge2'):>9.4f}"
              f"{agg(rows, 'top1'):>9.2f}{agg(rows, 'hits'):>9.4f}{agg(rows, 'sum'):>8.2f}")

    print()
    print("=" * 92)
    print("【二】按问法拆分（每种问法 20 例）")
    print(f"{'问法':<9}{'模式':<7}{'ndcg@10':>9}{'P@5':>8}{'首位≥2':>9}{'sum@10':>8}")
    for phrasing in PHRASINGS:
        for mode in MODES:
            rows = [v for k, v in per_case.items() if k[1] == phrasing and k[2] == mode]
            print(f"{LABEL_CN[phrasing]:<9}{MODE_CN[mode]:<7}{agg(rows, 'ndcg'):>9.4f}"
                  f"{agg(rows, 'p5'):>8.4f}{agg(rows, 'top1_ge2'):>9.4f}{agg(rows, 'sum'):>8.2f}")

    print()
    print("=" * 92)
    print("【三】逐条配对（同 JD 同问法，ndcg@10 差值）")
    for left, right in (("hybrid", "keyword"), ("vector", "keyword"), ("hybrid", "vector")):
        deltas, wins, losses = [], 0, 0
        for jid in results:
            for phrasing in PHRASINGS:
                a = per_case[(jid, phrasing, left)]["ndcg"]
                b = per_case[(jid, phrasing, right)]["ndcg"]
                if a is None or b is None:
                    continue
                deltas.append(a - b)
                wins += a > b
                losses += a < b
        mean = statistics.fmean(deltas)
        sd = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
        t = mean / (sd / math.sqrt(len(deltas))) if sd else 0.0
        print(f"{MODE_CN[left]} vs {MODE_CN[right]}: Δndcg={mean:+.4f} "
              f"胜/负={wins}/{losses} n={len(deltas)} t={t:+.2f}")

    print()
    print("=" * 92)
    print("【四】稳定性：每个 JD 内哪种模式在标准问法下最好")
    champion = collections.Counter()
    within = collections.Counter()
    for jid in results:
        vals = {m: per_case[(jid, "standard", m)]["ndcg"] or 0.0 for m in MODES}
        top = max(vals.values())
        winners = [m for m in MODES if abs(vals[m] - top) < 1e-9]
        for m in winners:
            champion[m] += 1 / len(winners)   # 并列按比例计
        for m in MODES:
            if top - vals[m] <= 0.02:
                within[m] += 1
    print("  并列折算后的冠军数：" + "  ".join(
        f"{MODE_CN[m]}={champion[m]:.1f}" for m in MODES))
    print("  与最佳差距 ≤0.02 的 JD 数（稳健性）：" + "  ".join(
        f"{MODE_CN[m]}={within[m]}/20" for m in MODES))
    print()
    print("  各模式标准问法 ndcg@10 的离散度：")
    for mode in MODES:
        vals = [per_case[(jid, "standard", mode)]["ndcg"] or 0.0 for jid in results]
        print(f"    {MODE_CN[mode]:<5} 均值={statistics.fmean(vals):.4f} "
              f"标准差={statistics.stdev(vals):.4f} 最差={min(vals):.4f} 最好={max(vals):.4f}")

    print()
    print("=" * 92)
    print("【五】失败类型分布：进入某模式前 10 名、但被判不匹配（grade≤1）的人，为什么错")
    print(f"{'模式':<7}{'条数':>7}  " + "".join(f"{FAIL_CN[t]:>12}" for t in FAIL_CN))
    for mode in MODES:
        tally = collections.Counter()
        total = 0
        for jid in results:
            table = judgements[jid]
            info = blinding[jid]
            for phrasing in PHRASINGS:
                for cand in results[jid][phrasing][mode][:10]:
                    letter = next((l for l, c in info["letters"].items() if c == cand), None)
                    entry = table.get(letter) if letter else None
                    if not entry:
                        continue
                    total += 1
                    if int(entry["grade"]) <= 1:
                        tally[entry.get("failure") or "未标注"] += 1
        shares = [f"{tally[t] * 100 / total:>11.1f}%" for t in FAIL_CN]
        print(f"{MODE_CN[mode]:<7}{total:>7}  " + "".join(shares))

    print()
    print("=" * 92)
    print("【六】按 JD 汇总（标准问法，ndcg@10）")
    print(f"{'JD':<5}{'岗位':<30}{'关键词':>9}{'向量':>9}{'混合':>9}{'最佳':>8}"
          f"{'grade≥2':>9}{'grade=3':>9}")
    degenerate = []
    for jid in sorted(results):
        info = blinding[jid]
        title = f"{info['company'][:8]}/{info['title'][:20]}"
        vals = {m: per_case[(jid, "standard", m)]["ndcg"] or 0.0 for m in MODES}
        strong = sum(1 for v in grades[jid].values() if v >= 2)
        perfect = sum(1 for v in grades[jid].values() if v >= 3)
        if strong == 0:
            degenerate.append(jid)
        bests = [m for m in MODES if abs(vals[m] - max(vals.values())) < 1e-9]
        print(f"{jid:<5}{title:<30}{vals['keyword']:>9.4f}{vals['vector']:>9.4f}"
              f"{vals['hybrid']:>9.4f}{MODE_CN[bests[0]]:>8}{strong:>9}{perfect:>9}")
    print(f"\n  注：这些 JD 在已判决候选池里没有任何 grade≥2 的人，其 ndcg 为退化值，"
          f"不应参与比较：{degenerate or '无'}")
    perfect_jds = [jid for jid in results
                   if not any(v >= 3 for v in grades[jid].values())]
    print(f"  完全没有 grade=3（可直接推荐）的 JD：{len(perfect_jds)}/20 -> {perfect_jds}")

    print()
    print("=" * 92)
    print("【七】判断方给出的「缺口」与「本批最佳」")
    for jid in sorted(results):
        print(f"{jid} 最佳={gaps[jid]['best']!r:<6} 缺口：{gaps[jid]['recall_gap']}")

    print()
    print("=" * 92)
    print("【八】分数分布（各模式前 10 名的分数直方图）")
    print(f"{'模式':<7}{'grade=0':>9}{'grade=1':>9}{'grade=2':>9}{'grade=3':>9}{'强相关占比':>11}")
    for mode in MODES:
        tally = collections.Counter()
        for jid in results:
            g = grades[jid]
            for phrasing in PHRASINGS:
                for cand in results[jid][phrasing][mode][:10]:
                    tally[g.get(cand, 0)] += 1
        total = sum(tally.values())
        strong = tally[2] + tally[3]
        print(f"{MODE_CN[mode]:<7}{tally[0]:>9}{tally[1]:>9}{tally[2]:>9}{tally[3]:>9}"
              f"{strong * 100 / total:>10.1f}%")

    (OUT / "judge_metrics.json").write_text(json.dumps(
        {f"{jid}|{p}|{m}": {k: v for k, v in row.items() if k != "seq"}
         for (jid, p, m), row in per_case.items()}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
