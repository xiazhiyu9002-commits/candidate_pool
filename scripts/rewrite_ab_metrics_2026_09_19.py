"""§1 第 3 步：还原盲测映射，算「改写关 vs 改写开」的配对指标。

同一 JD 的 off/on 两组结果**合并盲测**，故每个候选人只有一个评分，
配对检验只反映「是否被召回」，不掺判决方差。

指标（与上一轮判决式评测同口径）：
- nDCG@10（等级相关，2^k-1 增益）
- P@5(g≥2)：前 5 里「基本可用及以上」的比例
- 首位≥2：第 1 名的 grade ≥ 2
- hits@10：前 10 里 grade ≥ 2 的人数

配对检验：逐 JD 求 on − off 的差，报均值差、胜/负、配对 t 值。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AB = ROOT / ".tmp-judge" / "ab"
MODES = ("vector", "hybrid")
TOP_K = 10


def load_grades() -> dict[str, dict[str, int]]:
    grades: dict[str, dict[str, int]] = {}
    for n in range(1, 6):
        path = AB / f"judge_out_batch{n}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for jid, body in payload.items():
            grades[jid] = {item["label"]: int(item["grade"]) for item in body["judgements"]}
    return grades


def ndcg_at_k(letter_grades: list[int], k: int) -> float | None:
    gains = letter_grades[:k]
    if not gains or not any(g > 0 for g in gains):
        return None
    dcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted((g for g in letter_grades if g > 0), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else None


def metrics(letter_grades: list[int]) -> dict:
    top5 = letter_grades[:5]
    return {
        "ndcg@10": ndcg_at_k(letter_grades, TOP_K),
        "P@5(g>=2)": sum(1 for g in top5 if g >= 2) / 5 if top5 else 0.0,
        "首位>=2": 1.0 if letter_grades and letter_grades[0] >= 2 else 0.0,
        "hits@10": sum(1 for g in letter_grades[:TOP_K] if g >= 2),
    }


def mean(values) -> float:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else 0.0


def paired(deltas: list[float]) -> tuple[float, int, int, float]:
    n = len(deltas)
    avg = sum(deltas) / n if n else 0.0
    wins = sum(1 for d in deltas if d > 0)
    losses = sum(1 for d in deltas if d < 0)
    if n < 2:
        return avg, wins, losses, 0.0
    variance = sum((d - avg) ** 2 for d in deltas) / (n - 1)
    stderr = math.sqrt(variance / n)
    return avg, wins, losses, (avg / stderr if stderr else 0.0)


def main() -> None:
    blinding = json.loads((AB / "blinding_map.json").read_text(encoding="utf-8"))
    grades = load_grades()
    print(f"JD={len(blinding)}  已判决 JD={len(grades)}")

    arms = [f"{flag}_{mode}" for flag in ("off", "on") for mode in MODES]
    per_arm: dict[str, dict[str, list]] = {name: {"ndcg": [], "p5": [], "top1": [], "hits": []}
                                          for name in arms}
    per_jd: dict[str, dict[str, dict]] = {}

    for jid, body in blinding.items():
        letters = body["letters"]           # letter -> alias
        reverse = {alias: letter for letter, alias in letters.items()}
        judged = grades.get(jid, {})
        per_jd[jid] = {}
        for name, order in body["arms"].items():
            ranked = [judged[reverse[a]] for a in order if reverse.get(a) in judged]
            cell = metrics(ranked)
            per_jd[jid][name] = cell
            per_arm[name]["ndcg"].append(cell["ndcg@10"])
            per_arm[name]["p5"].append(cell["P@5(g>=2)"])
            per_arm[name]["top1"].append(cell["首位>=2"])
            per_arm[name]["hits"].append(cell["hits@10"])

    print(f"\n{'组别':<14}{'nDCG@10':>10}{'P@5(g>=2)':>12}{'首位>=2':>10}{'hits@10':>10}")
    for name in arms:
        data = per_arm[name]
        print(f"{name:<14}{mean(data['ndcg']):>10.4f}{mean(data['p5']):>12.4f}"
              f"{mean(data['top1']):>10.4f}{mean(data['hits']):>10.2f}")

    print(f"\n=== 配对检验（逐 JD，on − off）===")
    print(f"{'模式':<10}{'指标':<12}{'均值差':>10}{'胜/负':>10}{'配对 t':>10}")
    for mode in MODES:
        for key, label in (("ndcg@10", "nDCG@10"), ("P@5(g>=2)", "P@5(g>=2)"),
                           ("首位>=2", "首位>=2"), ("hits@10", "hits@10")):
            deltas = []
            for jid in per_jd:
                off = per_jd[jid][f"off_{mode}"][key]
                on = per_jd[jid][f"on_{mode}"][key]
                if off is None or on is None:
                    continue
                deltas.append(on - off)
            avg, wins, losses, t = paired(deltas)
            print(f"{mode:<10}{label:<12}{avg:>10.4f}{f'{wins}/{losses}':>10}{t:>10.2f}")

    # 召回变化：两组各自召回了多少「3 分」候选人（合格的强相关人）
    print(f"\n=== 强相关（grade=3）召回数 ===")
    for mode in MODES:
        off_total = on_total = 0
        for jid, body in blinding.items():
            judged = grades.get(jid, {})
            reverse = {alias: letter for letter, alias in body["letters"].items()}
            for name, total in ((f"off_{mode}", "off"), (f"on_{mode}", "on")):
                count = 0
                for alias in body["arms"][name]:
                    letter = reverse.get(alias)
                    if letter and judged.get(letter, 0) == 3:
                        count += 1
                if total == "off":
                    off_total += count
                else:
                    on_total += count
        print(f"  {mode}: off={off_total}  on={on_total}  "
              f"变化={on_total - off_total:+d}（{20 - 0} 个 JD 合计）")

    out = {
        "分组指标": {name: {k: round(mean(v), 4) for k, v in data.items()}
                     for name, data in per_arm.items()},
        "逐JD": per_jd,
    }
    (AB / "ab_metrics.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n产物={AB / 'ab_metrics.json'}")


if __name__ == "__main__":
    main()
