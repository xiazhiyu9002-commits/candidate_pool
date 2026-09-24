"""§1 判决式评测（AB 版）：把「改写关」与「改写开」两组结果**合并后一起盲测**。

为什么必须合并盲测（方案 §1.3）：同一 JD 的两次检索若有任何候选人只出现在一组里，
分开盲测会让判决口径漂移（同一人两次得分不同）。合并后每个候选人只被评分一次，
配对检验只反映「是否被召回」，不引入判决方差。

只比较 `vague`（模糊）问法 —— 这正是改写要救的场景。
候选摘要复用 ``judge_eval_build_input_2026_09_19`` 的 digest 口径，保证与上一轮可比。

产物（写入 ``.tmp-judge/ab/``）：
- ``judge_input_batch{1..N}.json`` 交给子 Agent（盲测，无模式/名次信息）
- ``blinding_map.json`` 评测者私有（字母 → 别名 + 两组各自的排序）

安全：只读数据库；候选人一律 sha256 别名。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev-data"
JUDGE = ROOT / ".tmp-judge"
PLAN = ROOT / ".tmp-plan"
OUT = JUDGE / "ab"
BATCH_SIZE = 4
MODES = ("vector", "hybrid")


def load_base_module():
    """复用既有构建器的 digest / load_candidates，避免两处口径漂移。"""
    path = ROOT / "scripts" / "judge_eval_build_input_2026_09_19.py"
    spec = importlib.util.spec_from_file_location("judge_build_base", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def main() -> None:
    base = load_base_module()
    off = json.loads((JUDGE / "search_results.json").read_text(encoding="utf-8"))
    on = json.loads((PLAN / "rewrite_on.json").read_text(encoding="utf-8"))
    jds = json.loads((JUDGE / "jd_selected.json").read_text(encoding="utf-8"))
    queries = json.loads((JUDGE / "queries.json").read_text(encoding="utf-8"))

    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    universe = base.load_candidates(connection)
    connection.close()
    by_alias = {alias(cid): cid for cid in universe}

    OUT.mkdir(parents=True, exist_ok=True)
    blinding: dict[str, dict] = {}
    batches: list[dict] = []
    batch: dict[str, dict] = {}
    sizes: list[int] = []

    for i, jd in enumerate(jds, 1):
        jid = f"J{i:02d}"
        arms = {
            "off_vector": off[jid]["vague"]["vector"],
            "off_hybrid": off[jid]["vague"]["hybrid"],
            "on_vector": on[jid]["vector"],
            "on_hybrid": on[jid]["hybrid"],
        }
        union: list[str] = []
        seen: set[str] = set()
        for mode in MODES:
            for cand in arms[f"off_{mode}"]:
                if cand not in seen:
                    seen.add(cand)
                    union.append(cand)
        for mode in MODES:
            for cand in arms[f"on_{mode}"]:
                if cand not in seen:
                    seen.add(cand)
                    union.append(cand)

        rng = random.Random(jd["alias"])
        shuffled = union[:]
        rng.shuffle(shuffled)
        letters = {cand: chr(ord("A") + n) for n, cand in enumerate(shuffled)}

        entries = []
        for cand in shuffled:
            cid = by_alias.get(cand)
            if cid is None:
                entries.append({"label": letters[cand], "digest": "（该候选人已不在库中）"})
                continue
            entry = dict(universe[cid])
            entry["cid"] = cid
            entries.append({"label": letters[cand], "digest": base.digest(entry)})

        batch[jid] = {
            "jd_id": jid,
            "company": jd["company"],
            "title": jd["title"],
            "min_years": jd["min_years"],
            "highest_degree": jd["highest_degree"],
            "location": jd["location"],
            "jd_text": jd["text"],
            "candidates": entries,
        }
        # 字母 → 别名；同时给出两组各自的**有序候选人别名**，供还原后算指标。
        blinding[jid] = {
            "jd_alias": jd["alias"],
            "company": jd["company"],
            "title": jd["title"],
            "letters": {letters[cand]: cand for cand in shuffled},
            "arms": {name: [cand for cand in order] for name, order in arms.items()},
            "candidate_count": len(shuffled),
            "vague_query": queries[jid]["vague"],
        }
        sizes.append(len(shuffled))

        if len(batch) >= BATCH_SIZE or i == len(jds):
            batches.append(batch)
            batch = {}

    for n, payload in enumerate(batches, 1):
        path = OUT / f"judge_input_batch{n}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"batch{n}: JD={list(payload)} 字符≈{path.stat().st_size // 1000}K -> {path.name}")

    (OUT / "blinding_map.json").write_text(
        json.dumps(blinding, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nJD={len(jds)} 去重候选总数={sum(sizes)} "
          f"每 JD min/median/max={min(sizes)}/{sorted(sizes)[len(sizes) // 2]}/{max(sizes)}")
    print(f"批次={len(batches)}  产物目录={OUT}")


if __name__ == "__main__":
    main()
