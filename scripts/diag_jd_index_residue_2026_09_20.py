"""只读核对：已软删除的 47 个岗位，是否还残留在「岗位索引」里。

为什么关键：反向匹配（人找岗位）直接查岗位索引。若索引里还留着已删除的岗位，
就会出现「推荐一个已经删掉的岗位」——这是会真正影响结果的残余，比数据库里留着更严重。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import lancedb  # noqa: E402

DB = (ROOT / ".dev-data" / "db" / "recruit.sqlite3").as_posix()
JOBS_INDEX = ROOT / ".dev-data" / "search" / "jobs"


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    live = {r["id"] for r in con.execute(
        "select r.id from jd_revision r join jd on jd.id = r.jd_id "
        "where r.is_current=1 and r.status='READY' and jd.deleted_at is null")}
    deleted = {r["id"] for r in con.execute(
        "select r.id from jd_revision r join jd on jd.id = r.jd_id "
        "where r.is_current=1 and r.status='READY' and jd.deleted_at is not null")}
    print(f"库内：未删除岗位 revision={len(live)}  已软删除={len(deleted)}")

    table = lancedb.connect(str(JOBS_INDEX)).open_table("candidate_chunks")
    arrow = table.search().select(["candidate_id", "revision_id", "chunk_type"]).limit(None).to_arrow()
    revisions = arrow.column("revision_id").to_pylist()
    chunk_types = arrow.column("chunk_type").to_pylist()
    unique = set(revisions)
    print(f"岗位索引：总行数={table.count_rows()}  唯一 revision={len(unique)}")
    from collections import Counter
    print(f"  chunk_type 分布：{dict(Counter(chunk_types))}")

    still_live = unique & live
    still_deleted = unique & deleted
    unknown = unique - live - deleted
    print(f"\n索引中的 revision 归属：")
    print(f"  未删除岗位（正常）      = {len(still_live)}")
    print(f"  **已软删除岗位（残留）** = {len(still_deleted)}")
    print(f"  库中已不存在（真残余）   = {len(unknown)}")

    missing = live - unique
    print(f"\n未删除但**未进索引**的岗位 = {len(missing)}")

    if unknown:
        print(f"\n=== 那 {len(unknown)} 个「库中已不存在」的索引条目详情 ===")
        for rev in sorted(unknown):
            row = con.execute(
                "select r.id, r.is_current, r.status, r.jd_id, jd.title, jd.deleted_at "
                "from jd_revision r left join jd on jd.id = r.jd_id where r.id = ?", (rev,)
            ).fetchone()
            if row is None:
                counts = sum(1 for value in revisions if value == rev)
                print(f"  {rev}  数据库无此行  索引中占 {counts} 行")
                continue
            print(f"  {rev}  is_current={row['is_current']} status={row['status']} "
                  f"jd_deleted={'是' if row['deleted_at'] else '否'}  {row['title']}")


if __name__ == "__main__":
    main()
