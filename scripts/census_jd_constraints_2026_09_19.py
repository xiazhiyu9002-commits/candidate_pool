"""只读普查：真实 JD 里 `exact_constraints`（AI 解析出的硬条件）的实际产出情况。

用途：判断「JD 硬门槛下推检索层」在当前真实数据上是否真的有作用 —— 如果 LLM
几乎不产出 MUST 硬条件，下推就是空转，方案 §2 的收益需要重新评估。

安全：`mode=ro` 只读打开数据库，不写任何文件。
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev-data"


def find_database() -> Path:
    candidates = [
        Path(root) / name
        for root, _dirs, files in os.walk(DEV)
        for name in files
        if name.endswith((".sqlite3", ".db", ".sqlite"))
    ]
    if not candidates:
        raise SystemExit(f"{DEV} 下没有找到数据库文件")
    return max(candidates, key=lambda p: p.stat().st_size)


def main() -> None:
    path = find_database()
    print(f"数据库: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "select parsed_data from jd_revision where is_current=1 and status='READY'"
    ).fetchall()
    total = len(rows)
    print(f"当前 READY 的 JD revision 数: {total}")

    kinds: collections.Counter = collections.Counter()
    strengths: collections.Counter = collections.Counter()
    with_any = 0
    with_must = 0
    samples: list[dict] = []

    for (raw,) in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        constraints = data.get("exact_constraints") or []
        if constraints:
            with_any += 1
        has_must = False
        for item in constraints:
            if not isinstance(item, dict):
                continue
            kinds[str(item.get("kind"))] += 1
            strength = str(item.get("strength") or "").upper()
            strengths[strength] += 1
            if strength == "MUST" and str(item.get("source_text") or "").strip():
                has_must = True
                if len(samples) < 5:
                    samples.append(item)
        if has_must:
            with_must += 1

    print(f"含 exact_constraints 的 JD        : {with_any} / {total}")
    print(f"含「MUST 且有原文依据」的 JD      : {with_must} / {total}")
    print(f"kind 分布     : {dict(kinds)}")
    print(f"strength 分布 : {dict(strengths)}")
    if samples:
        print("\n--- MUST 样例 ---")
        for item in samples:
            print(json.dumps(item, ensure_ascii=False))

    # 对照：requirements（另一个硬条件字段）是否真的承载了硬条件？
    # 若 requirements 有 MUST 而 exact_constraints 全空，说明硬条件进了 requirements，
    # 只是没有走 exact_constraints 这条下推路径。
    req_labels: collections.Counter = collections.Counter()
    req_kinds: collections.Counter = collections.Counter()
    with_req = 0
    for (raw,) in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        reqs = data.get("requirements") or []
        if reqs:
            with_req += 1
        for item in reqs:
            if isinstance(item, dict):
                req_kinds[str(item.get("kind"))] += 1
                req_labels[str(item.get("label"))] += 1
    print(f"\n含 requirements 的 JD : {with_req} / {total}")
    print(f"requirements kind 分布   : {dict(req_kinds)}")
    print(f"requirements label 分布  : {dict(req_labels.most_common(12))}")


if __name__ == "__main__":
    main()
