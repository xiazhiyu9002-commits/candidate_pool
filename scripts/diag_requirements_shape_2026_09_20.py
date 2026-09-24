"""只读：核对 requirements 条目的真实结构（字段名与取值），用于自查此前的统计口径。"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select parsed_data from jd_revision where is_current=1 and status='READY'"
    ).fetchall()

    key_counter: collections.Counter = collections.Counter()
    strength_like: collections.Counter = collections.Counter()
    samples = []
    total = 0
    for (raw,) in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in data.get("requirements") or []:
            if not isinstance(item, dict):
                continue
            total += 1
            key_counter.update(item.keys())
            for key in ("strength", "level", "priority", "requirement", "type",
                        "must", "kind", "category"):
                if key in item:
                    strength_like[f"{key}={item[key]!r}"] += 1
            if len(samples) < 4:
                samples.append(item)

    print(f"requirements 总条目：{total}")
    print(f"出现过的字段名：{dict(key_counter)}")
    print(f"\n类似 strength 的字段取值分布（前 20）：")
    for key, count in strength_like.most_common(20):
        print(f"  {key}: {count}")
    print("\n样例原文：")
    for item in samples:
        print(" ", json.dumps(item, ensure_ascii=False))

    print("\n=== must_skill_groups 样例（新版必备技能分组）===")
    shown = 0
    for (raw,) in rows:
        if not raw or shown >= 3:
            continue
        data = json.loads(raw)
        groups = data.get("must_skill_groups")
        if groups:
            print(" ", json.dumps(groups, ensure_ascii=False)[:300])
            shown += 1


if __name__ == "__main__":
    main()
