"""只读：把「外键未开启」造成的悬空引用彻底摸清，并取出那条悬空流程的详情。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name")]
    print("全部表：", ", ".join(tables))

    for candidate_table in ("correction_log", "index_sync", "index_sync_record"):
        if candidate_table in tables:
            print(f"\n{middle(candidate_table)}")
            cols = [r[1] for r in con.execute(f"pragma table_info({candidate_table})")]
            print(f"  字段：{cols}")
            print(f"  总行数：{con.execute(f'select count(*) from {candidate_table}').fetchone()[0]}")

    if "index_sync" in tables:
        rows = con.execute(
            "select count(*) from index_sync where entity_type='jd' and entity_id not in "
            "(select id from jd)").fetchone()[0]
        print(f"  index_sync 指向已删除岗位的：{rows}")
    if "correction_log" in tables:
        rows = con.execute(
            "select count(*) from correction_log where entity_type='jd' and entity_id not in "
            "(select id from jd)").fetchone()[0]
        print(f"  correction_log 指向已删除岗位的：{rows}")

    print("\n=== 悬空 match_run 明细 ===")
    total = con.execute(
        "select count(*) from match_run where jd_revision_id is not null and jd_revision_id not in "
        "(select id from jd_revision)").fetchone()[0]
    print(f"  悬空 match_run.jd_revision_id 共 {total} 条")
    rows = con.execute(
        "select trigger, count(*) from match_run where jd_revision_id is not null "
        "and jd_revision_id not in (select id from jd_revision) group by trigger").fetchall()
    print(f"  按 trigger 分布：{dict(rows)}")

    print("\n=== 悬空流程的详情与附属记录 ===")
    case_id = "01a08a56-3d8c-78d8-9db3-5d4bfed86547"
    row = con.execute(
        "select id, jd_id, candidate_id, stage, deleted_at, note, template_snapshot "
        "from candidate_job_case where id = ?", (case_id,)).fetchone()
    print(f"  case={row[0]}\n  jd_id={row[1]}（该岗位已被物理删除）\n  stage={row[3]}"
          f"\n  deleted_at={row[4]}\n  note={row[5]}\n  template_snapshot={'有' if row[6] else '无'}")
    for table, column in (("case_round", "case_id"), ("case_event", "case_id"),
                          ("stage_event", "case_id")):
        if table in tables:
            cols = [r[1] for r in con.execute(f"pragma table_info({table})")]
            if column in cols:
                count = con.execute(f"select count(*) from {table} where {column} = ?",
                                    (case_id,)).fetchone()[0]
                print(f"  附属 {table}: {count} 条")

    print("\n=== 22 条流程是否都被软删除 ===")
    rows = con.execute(
        "select coalesce(deleted_at, '未删除') as d, count(*) from candidate_job_case group by d"
    ).fetchall()
    print(f"  {dict(rows)}")
    print("\n=== 33 个岗位的流程占用 ===")
    rows = con.execute(
        "select jd_id, count(*) from candidate_job_case group by jd_id order by 2 desc limit 10"
    ).fetchall()
    print(f"  {dict(rows)}")


def middle(text: str) -> str:
    return f"=== {text} ==="


if __name__ == "__main__":
    main()
