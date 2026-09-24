"""只读：清理前的影响面统计。

关键问题：要清掉的 47 个岗位、208 个候选人，各自被多少「流程」引用？
- 候选人：`candidate_job_case.candidate_id` 是可空 + SET NULL，且已有快照字段 →
  现有 `CandidateDeletionService` 能保留流程。你要的语义它本来就满足。
- 岗位：`candidate_job_case.jd_id` 是 **NOT NULL + CASCADE**，且**没有岗位快照字段** →
  现有 `JdDeletionService` 只能「级联删掉流程」，做不到「保留流程」。
所以本脚本先把「这 47 个岗位到底挂了多少流程」查清楚，再决定能不能直接删。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def scalar(con: sqlite3.Connection, sql: str, params=()) -> int:
    row = con.execute(sql, params).fetchone()
    return row[0] if row else 0


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    jd_ids = [r[0] for r in con.execute("select id from jd where deleted_at is not null")]
    cand_ids = [r[0] for r in con.execute("select id from candidate where deleted_at is not null")]
    print(f"待清理：岗位 {len(jd_ids)} 个，候选人 {len(cand_ids)} 个\n")

    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    print(f"流程相关表是否存在：candidate_job_case={'candidate_job_case' in tables} "
          f"case_round={'case_round' in tables} case_event={'case_event' in tables} "
          f"stage_event={'stage_event' in tables}")

    def count_cases_by(column: str, ids: list[str]) -> tuple[int, int]:
        if not ids:
            return 0, 0
        total = 0
        deleted = 0
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            marks = ",".join("?" * len(batch))
            total += scalar(con, f"select count(*) from candidate_job_case where {column} in ({marks})", batch)
            deleted += scalar(
                con, f"select count(*) from candidate_job_case where {column} in ({marks}) "
                     f"and deleted_at is not null", batch)
        return total, deleted

    jd_cases, jd_cases_deleted = count_cases_by("jd_id", jd_ids)
    cand_cases, cand_cases_deleted = count_cases_by("candidate_id", cand_ids)
    print(f"\n47 个待删岗位  关联流程：{jd_cases} 条（其中已软删除 {jd_cases_deleted}）")
    print(f"208 个待删候选人 关联流程：{cand_cases} 条（其中已软删除 {cand_cases_deleted}）")

    print("\n=== 流程明细表规模（判断保留流程的成本）===")
    for table in ("case_round", "case_event", "stage_event"):
        if table in tables:
            print(f"  {table}: 总行数 {scalar(con, f'select count(*) from {table}')}")

    print("\n=== 岗位侧快照能力检查 ===")
    cols = [r[1] for r in con.execute("pragma table_info(candidate_job_case)")]
    jd_snapshot = [c for c in cols if c.startswith("jd_") and "snapshot" in c]
    print(f"  candidate_job_case 字段：{cols}")
    print(f"  岗位快照字段：{jd_snapshot or '（无）'}")
    nullability = {r[1]: (r[3], r[4]) for r in con.execute("pragma table_info(candidate_job_case)")}
    print(f"  candidate_id  (notnull, dflt) = {nullability.get('candidate_id')}")
    print(f"  jd_id         (notnull, dflt) = {nullability.get('jd_id')}")

    print("\n=== 岗位外键约束 ===")
    for row in con.execute("pragma foreign_key_list(candidate_job_case)"):
        print(f"  {row[3]} -> {row[2]}.{row[4]}  on_delete={row[6]}")

    print("\n=== 其余会受影响的关联 ===")
    marks = ",".join("?" * len(cand_ids))
    if "employee" in tables and cand_ids:
        print(f"  employee 绑定这些候选人的："
              f"{scalar(con, 'select count(*) from employee where candidate_id in (' + marks + ')', cand_ids)}")
    if "match_result" in tables:
        if jd_ids:
            jd_marks = ",".join("?" * len(jd_ids))
            jd_sql = ("select count(*) from match_result where jd_revision_id in "
                      "(select id from jd_revision where jd_id in (" + jd_marks + "))")
            print(f"  match_result 涉及这些岗位版本的：{scalar(con, jd_sql, jd_ids)}")
        if cand_ids:
            print(f"  match_result 涉及这些候选人的："
                  f"{scalar(con, 'select count(*) from match_result where candidate_id in (' + marks + ')', cand_ids)}")
    if "resume_document" in tables and cand_ids:
        print(f"  resume_document 归属这些候选人的："
              f"{scalar(con, 'select count(*) from resume_document where candidate_id in (' + marks + ')', cand_ids)}")
    if "resume_revision" in tables and cand_ids:
        sql = ("select count(*) from resume_revision where document_id in "
               "(select id from resume_document where candidate_id in (" + marks + "))")
        print(f"  resume_revision 归属这些候选人的：{scalar(con, sql, cand_ids)}")


if __name__ == "__main__":
    main()
