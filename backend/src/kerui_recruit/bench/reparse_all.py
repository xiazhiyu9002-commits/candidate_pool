"""离线批量重新解析：为所有 READY 简历/JD 版本入队 PARSE_RESUME / PARSE_JD 任务。

运行中的 8 并发 worker 会消费这些任务，用新的解析提示词重新解析；
解析完成后 parse_resume/parse_jd handler 会自动 enqueue_sync 重建该实体索引。

用法：
    py -3.12 -m kerui_recruit.bench.reparse_all --data-root "<repo>\\.dev-data"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.models import (
    Jd,
    JdRevision,
    ResumeDocument,
    ResumeRevision,
    TaskRecord,
)
from kerui_recruit.db.session import create_engine_for


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    args = parser.parse_args()

    database = Path(args.data_root) / "db" / "recruit.sqlite3"
    engine = create_engine_for(database)
    factory = sessionmaker(engine, expire_on_commit=False)

    resume_count = 0
    jd_count = 0
    with factory() as session, session.begin():
        revisions = session.scalars(
            select(ResumeRevision)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        for rev in revisions:
            key = f"REPARSE_V2:{rev.id}"
            exists = session.scalar(select(TaskRecord.id).where(TaskRecord.idempotency_key == key))
            if exists:
                continue
            session.add(TaskRecord(
                task_type="PARSE_RESUME",
                queue_name="interactive",
                priority=10,
                payload={"revision_id": rev.id, "force_ocr": False},
                idempotency_key=key,
            ))
            resume_count += 1

        jd_revisions = session.scalars(
            select(JdRevision)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(
                JdRevision.is_current.is_(True),
                JdRevision.status == "READY",
                Jd.status == "OPEN",
                Jd.deleted_at.is_(None),
            )
        ).all()
        for rev in jd_revisions:
            key = f"REPARSE_V2:{rev.id}"
            exists = session.scalar(select(TaskRecord.id).where(TaskRecord.idempotency_key == key))
            if exists:
                continue
            session.add(TaskRecord(
                task_type="PARSE_JD",
                queue_name="interactive",
                priority=10,
                payload={"revision_id": rev.id, "passive_match": False},
                idempotency_key=key,
            ))
            jd_count += 1

    print(f"enqueued: resume={resume_count}, jd={jd_count}")


if __name__ == "__main__":
    main()
