from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.models import ResumeRevision, TaskRecord
from kerui_recruit.db.session import create_engine_for

DB = Path(r"C:\Users\Nl\AppData\Local\KeRuiRecruit\db\recruit.sqlite3")
engine = create_engine_for(DB)
factory = sessionmaker(engine, expire_on_commit=False)

with factory() as session:
    revisions = session.scalars(
        select(ResumeRevision).where(ResumeRevision.is_current.is_(True))
    ).all()
    created = 0
    for rev in revisions:
        key = f"PARSE_RESUME:{rev.id}:v4"
        exists = session.scalar(select(TaskRecord).where(TaskRecord.idempotency_key == key))
        if exists is None:
            session.add(TaskRecord(
                task_type="PARSE_RESUME",
                queue_name="interactive",
                priority=10,
                payload={"revision_id": rev.id},
                idempotency_key=key,
            ))
            created += 1
    session.commit()
    print(f"total_revisions={len(revisions)} created_tasks={created}")
