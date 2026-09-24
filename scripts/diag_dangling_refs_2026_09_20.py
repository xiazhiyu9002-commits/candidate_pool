"""只读：清理后检查是否存在悬空引用（dangling FK）。

为什么必须查：SQLite 默认**不启用**外键约束（`PRAGMA foreign_keys=OFF`），
只有显式打开才会执行 ON DELETE CASCADE / SET NULL。清理脚本如果没打开，
`session.delete(jd)` 就不会级联删流程、也不会把 match_run.jd_revision_id 置空，
从而留下指向已删除岗位的悬空引用。

本脚本逐个检查所有可能引用 jd / jd_revision / candidate 的列。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()

# (表, 列, 被引用的表)
REFERENCES = [
    ("candidate_job_case", "jd_id", "jd"),
    ("candidate_job_case", "candidate_id", "candidate"),
    ("match_run", "jd_revision_id", "jd_revision"),
    ("match_result", "jd_revision_id", "jd_revision"),
    ("match_result", "candidate_id", "candidate"),
    ("jd_revision", "jd_id", "jd"),
    ("case_round", "case_id", "candidate_job_case"),
    ("case_event", "case_id", "candidate_job_case"),
    ("resume_document", "candidate_id", "candidate"),
    ("resume_revision", "document_id", "resume_document"),
    ("employee", "candidate_id", "candidate"),
    ("index_sync_record", "entity_id", None),
]


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    print(f"PRAGMA foreign_keys（本连接，只读默认值）= "
          f"{con.execute('pragma foreign_keys').fetchone()[0]}")

    print("\n=== 悬空引用检查 ===")
    problems = 0
    for table, column, target in REFERENCES:
        if table not in tables:
            print(f"  跳过 {table}（表不存在）")
            continue
        cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
        if column not in cols:
            print(f"  跳过 {table}.{column}（列不存在）")
            continue
        if target is None:
            continue
        if target not in tables:
            print(f"  跳过 {table}.{column}（目标表 {target} 不存在）")
            continue
        if column == "candidate_id" and target == "candidate":
            # 允许为 NULL（候选人删除后快照保留）
            sql = (f"select count(*) from {table} t where t.{column} is not null "
                   f"and not exists (select 1 from {target} x where x.id = t.{column})")
        else:
            sql = (f"select count(*) from {table} t where t.{column} is not null "
                   f"and not exists (select 1 from {target} x where x.id = t.{column})")
        count = con.execute(sql).fetchone()[0]
        flag = "  ← 悬空！" if count else ""
        if count:
            problems += count
        print(f"  {table}.{column} -> {target}: 悬空 {count}{flag}")

    print(f"\n悬空引用合计：{problems}")

    print("\n=== 当前规模 ===")
    for table in ("jd", "jd_revision", "candidate", "candidate_job_case",
                  "match_run", "match_result", "case_round", "case_event"):
        if table in tables:
            print(f"  {table}: {con.execute(f'select count(*) from {table}').fetchone()[0]}")

    print("\n=== 那条流程的现状 ===")
    rows = con.execute(
        "select id, jd_id, candidate_id, stage, deleted_at, note from candidate_job_case "
        "where jd_id not in (select id from jd)").fetchall()
    for row in rows:
        print(f"  case={row[0]} jd_id={row[1]} candidate_id={row[2]} stage={row[3]} "
              f"deleted_at={row[4]}")


if __name__ == "__main__":
    main()
