"""止血：把 skill / other_keyword 的 MUST 硬条件降级为 PLUS。

原因：模型对「必须熟悉 Google Vertex AI SDK 或 Google GenAI SDK」这类长短语/产品名产出 MUST，
而判定层对每条 MUST 都是 AND（全部必须满足）→ 库里无人具备 → 整库被拒、返回 0。
学历 / 学校档次 / 公司经历是客观可核验的属性，保留 MUST；技能类短语匹配噪声大，降级为 PLUS。

用法：
    python scripts/downgrade_skill_must_2026_09_20.py            # dry-run
    python scripts/downgrade_skill_must_2026_09_20.py --apply    # 写库
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

# 允许保留 MUST 的 kind：客观、可结构化核验，且判定口径与索引列一一对应。
ALLOWED_MUST_KINDS = {"degree", "school_level", "company_history"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    engine = create_engine_for(ROOT / ".dev-data" / "db" / "recruit.sqlite3")
    changed = total = 0
    with Session(engine) as session:
        revisions = session.scalars(
            select(JdRevision)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.deleted_at.is_(None))
        ).all()
        for revision in revisions:
            data = dict(revision.parsed_data or {})
            items = [c for c in (data.get("exact_constraints") or []) if isinstance(c, dict)]
            downgraded = []
            for index, item in enumerate(items):
                kind = str(item.get("kind") or "")
                if str(item.get("strength") or "").upper() == "MUST" and kind not in ALLOWED_MUST_KINDS:
                    replaced = dict(item)
                    replaced["strength"] = "PLUS"
                    items[index] = replaced
                    downgraded.append((kind, item.get("alternatives")))
            if not downgraded:
                continue
            changed += 1
            total += len(downgraded)
            print(f"《{revision.jd.title}》 降级 {len(downgraded)} 条 MUST → PLUS")
            for kind, alternatives in downgraded:
                print(f"    - {kind}: {alternatives}")
            if args.apply:
                data["exact_constraints"] = items
                revision.parsed_data = data
        if args.apply:
            session.commit()
    mode = "已写入" if args.apply else "dry-run（未写库）"
    print(f"\n{mode}：{changed} 个 JD / 共 {total} 条 MUST 降级为 PLUS。")


if __name__ == "__main__":
    main()
