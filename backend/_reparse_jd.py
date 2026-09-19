import sys
from pathlib import Path
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.models import Jd, JdRevision, TaskRecord

engine = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
factory = sessionmaker(engine, expire_on_commit=False)

count = 0
with factory() as session, session.begin():
    revs = session.scalars(
        select(JdRevision).join(Jd, Jd.id == JdRevision.jd_id).where(
            JdRevision.is_current.is_(True),
            JdRevision.status == "READY",
            Jd.status == "OPEN",
            Jd.deleted_at.is_(None),
        )
    ).all()
    for rev in revs:
        key = f"REPARSE_DIRECTION:{rev.id}"
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
        count += 1

print(f"enqueued JD: {count}")
