from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from kerui_recruit.db.models import Jd, JdRevision, TaskRecord


@dataclass(frozen=True, slots=True)
class IngestJd:
    company: str
    title: str
    source_text: str
    queue_name: str = "interactive"
    passive_match: bool = True


@dataclass(frozen=True, slots=True)
class JdIngestResult:
    jd_id: str
    revision_id: str


class JdIngestService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest(self, command: IngestJd) -> JdIngestResult:
        jd = Jd(company=command.company, title=command.title, status="OPEN")
        self.session.add(jd)
        self.session.flush()
        revision = JdRevision(
            jd=jd,
            revision_no=1,
            source_text=command.source_text,
            status="PENDING",
            is_current=True,
        )
        self.session.add(revision)
        self.session.flush()
        task = TaskRecord(
            task_type="PARSE_JD",
            queue_name=command.queue_name,
            priority=10,
            payload={
                "revision_id": revision.id,
                "passive_match": command.passive_match,
            },
            idempotency_key=f"PARSE_JD:{revision.id}:v1",
        )
        self.session.add(task)
        self.session.commit()
        return JdIngestResult(jd_id=jd.id, revision_id=revision.id)