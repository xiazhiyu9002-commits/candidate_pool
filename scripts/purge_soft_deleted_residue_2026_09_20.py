"""一次性清理：物理清除「改成硬删除之前」遗留的软删除记录。

范围（用户已确认，且明确**不做备份**）：
1. `jd`：deleted_at 非空的 47 个 → `JdDeletionService`
   （级联清 JdRevision / JdRequirement / CandidateJobCase，显式清 MatchResult /
    CorrectionLog / IndexSyncRecord，并清岗位索引）
2. `candidate`：deleted_at 非空的 208 个 → `CandidateDeletionService`
   （已实测：这 208 个无流程、无简历文档、无员工绑定、无匹配结果 → 零牵连）
3. 岗位索引里「数据库已不存在」的孤儿 revision → 直接按 revision 删索引条目

为什么复用服务而不是写 SQL：关联清理逻辑（版本/要求/流程/匹配结果/索引/纠错记录）
已经写在服务里，手写 DELETE 极易漏表。

安全：只操作 `.dev-data`；先打印 before 计数，删完打印 after 计数与校验。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.match.jd_index import JdSearchIndex  # noqa: E402
from kerui_recruit.resumes.deletion import CandidateDeletionService  # noqa: E402
from kerui_recruit.jd.deletion import JdDeletionService  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402

DEV = ROOT / ".dev-data"
DB = DEV / "db" / "recruit.sqlite3"


def counts(engine) -> dict:
    with engine.connect() as con:
        def one(sql: str) -> int:
            return con.execute(text(sql)).scalar_one()
        return {
            "jd 总数": one("select count(*) from jd"),
            "jd 已软删除": one("select count(*) from jd where deleted_at is not null"),
            "candidate 总数": one("select count(*) from candidate"),
            "candidate 已软删除": one("select count(*) from candidate where deleted_at is not null"),
            "jd_revision 总数": one("select count(*) from jd_revision"),
            "match_result 总数": one("select count(*) from match_result"),
            "candidate_job_case 总数": one("select count(*) from candidate_job_case"),
        }


def index_revisions(index: LanceDBSearchIndex) -> set[str]:
    table = index.database.open_table(index.table_name)
    arrow = table.search().select(["revision_id"]).limit(None).to_arrow()
    return set(arrow.column("revision_id").to_pylist())


def main() -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    model = settings["siliconflow_embedding_model"]

    # 必须用生产同一个引擎构造器：它会在每个连接上 PRAGMA foreign_keys=ON。
    # 裸 create_engine 默认不开外键 → ON DELETE CASCADE / SET NULL 全部静默失效，
    # 会留下悬空引用（本脚本第一版就踩了这个坑，已修正）。
    engine = create_engine_for(DB)
    factory = sessionmaker(bind=engine)

    candidate_index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                                         embedding_model=model)
    jd_index = JdSearchIndex(DEV / "search" / "jobs", vector_dimension=1024,
                             embedding_model=model)

    print("=== before ===")
    before = counts(engine)
    for key, value in before.items():
        print(f"  {key}: {value}")

    with engine.connect() as con:
        jd_ids = [r[0] for r in con.execute(text("select id from jd where deleted_at is not null"))]
        cand_ids = [r[0] for r in con.execute(
            text("select id from candidate where deleted_at is not null"))]
        # 前置断言：确认待删候选人确实没有简历文档（否则需带上 blob / 任务清理）。
        if cand_ids:
            marks = ",".join(f"'{value}'" for value in cand_ids)
            docs = con.execute(text(
                f"select count(*) from resume_document where candidate_id in ({marks})")).scalar_one()
            if docs:
                raise SystemExit(f"中止：待删候选人名下仍有 {docs} 份简历文档，需带 blob 清理重做")
            cases = con.execute(text(
                f"select count(*) from candidate_job_case where candidate_id in ({marks})")).scalar_one()
            if cases:
                raise SystemExit(f"中止：待删候选人仍关联 {cases} 条流程，需确认保留策略")
    print(f"\n前置断言通过：{len(cand_ids)} 个待删候选人无简历文档、无流程关联")
    print(f"待删岗位 {len(jd_ids)} 个")

    print("\n=== 1/3 物理删除岗位 ===")
    jd_deletion = JdDeletionService(factory, jd_index=jd_index)
    jd_ok = 0
    for i, jd_id in enumerate(jd_ids, 1):
        try:
            if jd_deletion.delete(jd_id):
                jd_ok += 1
        except Exception as error:  # noqa: BLE001
            print(f"  失败 {jd_id}: {type(error).__name__}: {error}")
        if i % 10 == 0 or i == len(jd_ids):
            print(f"  进度 {i}/{len(jd_ids)}，成功 {jd_ok}", flush=True)

    print("\n=== 2/3 物理删除候选人 ===")
    cand_deletion = CandidateDeletionService(factory, candidate_index)
    cand_ok = 0
    for i, cand_id in enumerate(cand_ids, 1):
        try:
            if cand_deletion.delete(cand_id):
                cand_ok += 1
        except Exception as error:  # noqa: BLE001
            print(f"  失败 {cand_id}: {type(error).__name__}: {error}")
        if i % 50 == 0 or i == len(cand_ids):
            print(f"  进度 {i}/{len(cand_ids)}，成功 {cand_ok}", flush=True)

    print("\n=== 3/3 清理岗位索引孤儿 ===")
    with engine.connect() as con:
        live_revisions = {r[0] for r in con.execute(text("select id from jd_revision"))}
    indexed = index_revisions(jd_index.index)
    orphans = indexed - live_revisions
    print(f"  索引中 revision={len(indexed)}  库中 revision={len(live_revisions)}  "
          f"孤儿={len(orphans)}")
    for revision in sorted(orphans):
        jd_index.index.delete_revision(revision)
    print(f"  已删除孤儿索引条目 {len(orphans)} 条")

    print("\n=== after ===")
    after = counts(engine)
    for key, value in after.items():
        delta = before[key] - value
        print(f"  {key}: {value}   （减少 {delta}）")

    indexed_after = index_revisions(jd_index.index)
    print(f"  岗位索引唯一 revision: {len(indexed_after)}"
          f"   （清理前 {len(indexed)}）")
    orphan_after = indexed_after - live_revisions
    print(f"  岗位索引孤儿（应为 0）: {len(orphan_after)}")

    print("\n=== 校验 ===")
    print(f"  jd 已软删除残留（应为 0）      : {after['jd 已软删除']}")
    print(f"  candidate 已软删除残留（应为 0）: {after['candidate 已软删除']}")
    print(f"  jd 剩 {after['jd 总数']}（期望 33）；candidate 剩 {after['candidate 总数']}（期望 1516）")
    print(f"  岗位索引 {len(indexed_after)} 个 revision（期望 33）")
    if after["jd 已软删除"] == 0 and after["candidate 已软删除"] == 0 and not orphan_after:
        print("\n清理完成且校验通过。")
    else:
        print("\n仍有残留，请检查上面的失败行。")


if __name__ == "__main__":
    main()
