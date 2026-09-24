"""只读普查：岗位（JD）侧解析产出的完整覆盖率与词表使用情况。

回答两个问题：
1. 职业方向/业务方向的取值词表是否简历与 JD 共用（同集合）→ 对照观测值 vs 全量词表；
2. 岗位解析这一侧到底哪些字段是空的、哪些字段产出了但没人用。

同时检查 `career_taxonomy_version`，用于识别「解析结果是旧提示词版本留下的」这种情况。
"""
from __future__ import annotations

import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.direction.policy import (  # noqa: E402
    BUSINESS_DIRECTIONS,
    CAREER_SPECIALIZATIONS,
    TAXONOMY_VERSION,
    VALID_DIRECTIONS,
)

DB = (ROOT / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, dict)):
        return len(value) == 0
    return False


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select r.id, r.created_at, r.parsed_data from jd_revision r "
        "join jd on jd.id = r.jd_id "
        "where r.is_current=1 and r.status='READY' and jd.deleted_at is null"
    ).fetchall()
    print(f"**活跃** JD revision（未软删除）：{len(rows)} 条   词表版本常量 TAXONOMY_VERSION={TAXONOMY_VERSION}")
    print("注：早前把已软删除的岗位也算进了分母，导致覆盖率被低估。本脚本只统计活跃岗位。")

    total = len(rows)
    coverage: collections.Counter = collections.Counter()
    versions: collections.Counter = collections.Counter()
    business_values: collections.Counter = collections.Counter()
    career_values: collections.Counter = collections.Counter()
    spec_values: collections.Counter = collections.Counter()
    requirement_labels: collections.Counter = collections.Counter()
    requirement_strengths: collections.Counter = collections.Counter()
    cross: collections.Counter = collections.Counter()
    parsed_rows = 0
    created_range = []

    for _id, created_at, raw in rows:
        created_range.append(str(created_at)[:10])
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        parsed_rows += 1
        for key, value in data.items():
            if not empty(value):
                coverage[key] += 1
        versions[str(data.get("career_taxonomy_version"))] += 1

        business = [str(v) for v in (data.get("business_directions") or []) if v]
        career = [str(v) for v in (data.get("career_directions") or []) if v]
        specs = [str(v) for v in (data.get("career_specializations") or []) if v]
        business_values.update(business)
        career_values.update(career)
        spec_values.update(specs)
        cross[(bool(career), bool(business))] += 1

        for item in data.get("requirements") or []:
            if isinstance(item, dict):
                requirement_labels[str(item.get("label"))] += 1
                requirement_strengths[str(item.get("strength") or "").upper()] += 1

    print(f"可解析 parsed_data 的：{parsed_rows} 条；修订创建日期范围 {min(created_range)} ~ {max(created_range)}")

    print("\n=== 字段覆盖率（非空占比）===")
    for key, count in coverage.most_common():
        print(f"  {key:<32}{count:>4}/{parsed_rows}  {count / max(1, parsed_rows) * 100:>5.1f}%")

    print("\n=== career_taxonomy_version 分布 ===")
    for version, count in versions.most_common():
        flag = "" if version == TAXONOMY_VERSION else "   ← 与当前词表版本不一致"
        print(f"  {version}: {count}{flag}")

    print(f"\n=== 词表是同一份吗 ===")
    print(f"  职业方向大类词表（共用）  {len(VALID_DIRECTIONS)} 个：{VALID_DIRECTIONS}")
    print(f"  JD 侧实际出现过        {len(career_values)} 种：{sorted(career_values)}")
    print(f"  职业细分词表（共用）      {len(CAREER_SPECIALIZATIONS)} 个")
    print(f"  JD 侧实际出现过        {len(spec_values)} 种")
    print(f"  业务方向词表（共用）      {len(BUSINESS_DIRECTIONS)} 个")
    print(f"  JD 侧实际出现过        {len(business_values)} 种：{dict(business_values)}")
    missing = [v for v in BUSINESS_DIRECTIONS if v not in business_values]
    print(f"  业务方向全量词表中 JD 侧从未出现的 {len(missing)} 个：{missing}")

    print("\n=== 职业方向 × 业务方向 的联合覆盖（JD 侧）===")
    for (has_career, has_business), count in sorted(cross.items()):
        print(f"  职业={'有' if has_career else '无'} 业务={'有' if has_business else '无'}: {count} 条")

    print("\n=== requirements（要求清单）统计 ===")
    print(f"  总量：{sum(requirement_strengths.values())} 条")
    print(f"  strength 分布：{dict(requirement_strengths)}")
    print(f"  label 分布：{dict(requirement_labels.most_common(15))}")


if __name__ == "__main__":
    main()
