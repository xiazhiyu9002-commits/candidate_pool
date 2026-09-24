"""回填 JD 画像的 exact_constraints（规则兜底路径）。

背景：`parse_exact_constraints` 之前只在手动接口被调用，画像文本里写的硬条件
（「985 本科及以上」「本科及以上学历」「必须有字节背景」）从未进入系统。本脚本对活跃 JD
的 `candidate_profile + summary` 跑一次规则抽取，与已有约束合并后写回 `parsed_data`。

用法：
    python scripts/backfill_exact_constraints_2026_09_20.py            # dry-run（默认，只读）
    python scripts/backfill_exact_constraints_2026_09_20.py --apply    # 写库

安全：去重合并（不覆盖已有约束）；只做加法；`exact_constraints` 不参与索引文档构建，
写库后**无需**重建索引或重新同步。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kerui_recruit.db.models import Jd, JdRevision  # noqa: E402
from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.jd.profile_constraints import parse_exact_constraints  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402

DEV = ROOT / ".dev-data"


def constraint_key(item: dict) -> tuple:
    return (str(item.get("kind")), str(item.get("strength")).upper(),
            tuple(str(a) for a in (item.get("alternatives") or ())))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="写库（默认只做 dry-run）")
    args = parser.parse_args()

    engine = create_engine_for(DEV / "db" / "recruit.sqlite3")
    changed = 0
    with Session(engine) as session:
        revisions = session.scalars(
            select(JdRevision)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.deleted_at.is_(None))
        ).all()
        for revision in revisions:
            data = dict(revision.parsed_data or {})
            text = "\n".join(str(x) for x in (data.get("candidate_profile") or "", data.get("summary") or "") if x)
            if not text:
                continue
            existing = [item for item in (data.get("exact_constraints") or []) if isinstance(item, dict)]
            seen = {constraint_key(item) for item in existing}
            added = []
            for constraint in parse_exact_constraints(text, source="inferred"):
                candidate = {
                    "kind": constraint.kind, "operator": constraint.operator,
                    "alternatives": list(constraint.alternatives),
                    "strength": constraint.strength, "source": constraint.source,
                    "source_text": constraint.source_text,
                }
                if constraint_key(candidate) in seen:
                    continue
                seen.add(constraint_key(candidate))
                added.append(candidate)
            if not added:
                continue
            changed += 1
            print(f"《{revision.jd.title}》 已有 {len(existing)} 条 → 新增 {len(added)} 条")
            for item in added:
                print(f"    + {item['kind']}/{item['strength']} {item['alternatives']}  ← {item['source_text'][:50]}")
            if args.apply:
                data["exact_constraints"] = [*existing, *added]
                revision.parsed_data = data
        if args.apply:
            session.commit()
    mode = "已写入" if args.apply else "dry-run（未写库）"
    print(f"\n{mode}：共 {changed} 个 JD 会产生新增约束。")


if __name__ == "__main__":
    main()
