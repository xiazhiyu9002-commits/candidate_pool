"""修复：补做「外键未开启」导致漏掉的级联动作。

背景：清理脚本误用裸 `create_engine`，未开 `PRAGMA foreign_keys=ON`（生产在
`kerui_recruit.db.session.create_engine_for` 里显式开启），因此：
1. `candidate_job_case.jd_id` 的 `ON DELETE CASCADE` 未执行 → 1 条流程悬空；
2. `match_run.jd_revision_id` 的 `ON DELETE SET NULL` 未执行 → 64 条匹配批次悬空。

本脚本用**生产同一个引擎构造器**（FK 已开启）补做这两件事：
- `match_run` 的悬空外键置空（等价于补做 SET NULL）
- 悬空流程行删除（等价于补做 CASCADE；该流程无轮次/事件/备注，删除无信息损失）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import text  # noqa: E402

from kerui_recruit.db.session import create_engine_for  # noqa: E402

DB = ROOT / ".dev-data" / "db" / "recruit.sqlite3"

DANGLING_RUN = (
    "select count(*) from match_run where jd_revision_id is not null "
    "and jd_revision_id not in (select id from jd_revision)"
)
DANGLING_CASE = (
    "select count(*) from candidate_job_case where jd_id not in (select id from jd)"
)


def main() -> None:
    engine = create_engine_for(DB)
    with engine.begin() as con:
        fk = con.execute(text("pragma foreign_keys")).scalar_one()
        print(f"PRAGMA foreign_keys = {fk}（生产引擎应为 1）")
        if fk != 1:
            raise SystemExit("外键未开启，中止")

        runs_before = con.execute(text(DANGLING_RUN)).scalar_one()
        cases_before = con.execute(text(DANGLING_CASE)).scalar_one()
        print(f"\n修复前：悬空 match_run={runs_before}  悬空流程={cases_before}")

        updated = con.execute(text(
            "update match_run set jd_revision_id = null "
            "where jd_revision_id is not null "
            "and jd_revision_id not in (select id from jd_revision)"
        )).rowcount
        print(f"  match_run 置空 {updated} 条")

        case_ids = [r[0] for r in con.execute(text(
            "select id from candidate_job_case where jd_id not in (select id from jd)"))]
        for case_id in case_ids:
            con.execute(text("delete from candidate_job_case where id = :id"), {"id": case_id})
        print(f"  删除悬空流程 {len(case_ids)} 条：{case_ids}")

    with engine.connect() as con:
        runs_after = con.execute(text(DANGLING_RUN)).scalar_one()
        cases_after = con.execute(text(DANGLING_CASE)).scalar_one()
        print(f"\n修复后：悬空 match_run={runs_after}  悬空流程={cases_after}")

        print("\n=== 残留规模 ===")
        for table in ("jd", "jd_revision", "candidate", "candidate_job_case",
                      "match_run", "match_result"):
            value = con.execute(text(f"select count(*) from {table}")).scalar_one()
            print(f"  {table}: {value}")

    print("\n" + ("修复完成，悬空引用已清零。" if runs_after == 0 and cases_after == 0
                  else "仍有悬空，请检查。"))


if __name__ == "__main__":
    main()
