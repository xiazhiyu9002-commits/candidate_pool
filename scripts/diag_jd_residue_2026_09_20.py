"""只读核对：那 47 个「旧解析」JD 到底是什么状态 —— 软删除、真残余，还是正常在库？

同时检查是否存在孤立数据（jd_revision 指向不存在的 jd）。
不做任何写操作。
"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=== jd 表 ===")
    total = con.execute("select count(*) from jd").fetchone()[0]
    deleted = con.execute("select count(*) from jd where deleted_at is not null").fetchone()[0]
    print(f"  jd 总数={total}  其中 deleted_at 非空={deleted}  未删除={total - deleted}")
    print("  status 分布（全部）：",
          dict(con.execute("select status, count(*) from jd group by status").fetchall()))
    print("  status 分布（未删除）：",
          dict(con.execute(
              "select status, count(*) from jd where deleted_at is null group by status").fetchall()))

    print("\n=== jd_revision 表 ===")
    rev_total = con.execute("select count(*) from jd_revision").fetchone()[0]
    rev_ready = con.execute(
        "select count(*) from jd_revision where is_current=1 and status='READY'").fetchone()[0]
    print(f"  jd_revision 总数={rev_total}  当前且 READY={rev_ready}")

    live = con.execute(
        "select count(*) from jd_revision r join jd on jd.id = r.jd_id "
        "where r.is_current=1 and r.status='READY' and jd.deleted_at is null"
    ).fetchone()[0]
    print(f"  当前 READY **且父 JD 未删除** = {live}")
    print(f"  → 落在已删除 JD 上的 = {rev_ready - live}")

    print("\n=== 那 80 条按「父 JD 是否已删除」拆开，看词表版本 ===")
    rows = con.execute(
        "select r.parsed_data, jd.deleted_at, jd.status "
        "from jd_revision r join jd on jd.id = r.jd_id "
        "where r.is_current=1 and r.status='READY'"
    ).fetchall()
    cross: collections.Counter = collections.Counter()
    for row in rows:
        version = None
        try:
            version = (json.loads(row["parsed_data"]) if row["parsed_data"] else {}).get(
                "career_taxonomy_version")
        except (TypeError, ValueError):
            version = None
        cross[("已删除" if row["deleted_at"] else "未删除", row["status"], version)] += 1
    for key, count in sorted(cross.items(), key=lambda kv: -kv[1]):
        print(f"  {key[0]}  jd.status={key[1]:<8} taxonomy_version={key[2]}: {count} 条")

    print("\n=== 孤立数据检查 ===")
    orphan_rev = con.execute(
        "select count(*) from jd_revision r left join jd on jd.id = r.jd_id "
        "where jd.id is null").fetchone()[0]
    print(f"  jd_revision 指向不存在的 jd（真残余）= {orphan_rev}")
    orphan_run = con.execute(
        "select count(*) from match_run m left join jd_revision r on r.id = m.jd_revision_id "
        "where m.jd_revision_id is not null and r.id is null").fetchone()[0]
    print(f"  match_run 指向不存在的 jd_revision = {orphan_run}")
    orphan_result = con.execute(
        "select count(*) from match_result x left join jd_revision r on r.id = x.jd_revision_id "
        "where x.jd_revision_id is not null and r.id is null").fetchone()[0]
    print(f"  match_result 指向不存在的 jd_revision = {orphan_result}")

    print("\n=== 回收站能力（软删除是设计功能，不是残余）===")
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and "
        "(name like '%trash%' or name like '%recycle%' or name like '%purge%')")]
    print(f"  相关表：{tables or '（无专用表，软删除直接写在 deleted_at 字段）'}")
    columns = [r[1] for r in con.execute("pragma table_info(jd)")]
    print(f"  jd 表字段：{columns}")


if __name__ == "__main__":
    main()
