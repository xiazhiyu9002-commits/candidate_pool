from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import CorrectionLog, JdRevision, ResumeDocument, ResumeRevision, TaskRecord
from kerui_recruit.cases.state import refresh_links
from kerui_recruit.search.sync import enqueue_sync


class CorrectionConflict(ValueError):
    code = "E_CORRECTION_PROCESSING"


_CORRECTABLE_FIELDS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"display_name", "status", "total_years", "highest_degree"}),
    "jd": frozenset({"company", "title", "status", "priority"}),
    "jd_revision": frozenset({"source_text"}),
    "resume_revision": frozenset({"display_name"}),
}


class CorrectionService:
    """Append-only correction log with undo support.

    Every field mutation is recorded so users can revert individual
    corrections later.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def apply_correction(
        self,
        *,
        entity_type: str,
        entity_id: str,
        field_name: str,
        new_value: str | None,
        reason: str | None = None,
    ) -> CorrectionLog:
        _validate_field(entity_type, field_name)
        with self.session_factory() as session:
            old_value = self._current_value(session, entity_type, entity_id, field_name)

            log = CorrectionLog(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                reason=reason,
            )
            session.add(log)
            session.flush()
            self._set_field(session, entity_type, entity_id, field_name, new_value, operation_id=log.id)
            session.commit()
            return log

    def undo_correction(self, correction_id: str) -> CorrectionLog:
        with self.session_factory() as session:
            log = session.get(CorrectionLog, correction_id)
            if log is None:
                raise LookupError(f"CorrectionLog not found: {correction_id}")
            if log.reverted:
                raise ValueError(f"Correction already reverted: {correction_id}")

            self._set_field(
                session, log.entity_type, log.entity_id, log.field_name, log.old_value,
                operation_id=f"{log.id}:undo",
            )
            log.reverted = True
            log.reverted_at = datetime.now(timezone.utc)
            session.commit()
            return log

    @staticmethod
    def _current_value(
        session: Session, entity_type: str, entity_id: str, field_name: str
    ) -> str | None:
        entity = _lookup(session, entity_type, entity_id)
        val = getattr(entity, field_name, None)
        return str(val) if val is not None else None

    @staticmethod
    def _set_field(
        session: Session,
        entity_type: str,
        entity_id: str,
        field_name: str,
        value: Any,
        *, operation_id: str,
    ) -> None:
        _validate_field(entity_type, field_name)
        entity = _lookup(session, entity_type, entity_id)
        if entity_type == "jd_revision":
            running = session.scalar(select(TaskRecord.id).where(
                TaskRecord.task_type == "PARSE_JD", TaskRecord.status == "RUNNING",
                TaskRecord.payload["revision_id"].as_string() == entity_id,
            ).limit(1))
            if entity.status == "PROCESSING" or running is not None:
                raise CorrectionConflict("岗位正在解析，请解析完成后再更正或撤销")
            entity.source_text = value
            entity.status = "PENDING"
            entity.parsed_data = None
            entity.ai_category = entity.highest_degree = entity.min_years = entity.location = None
            entity.requirements = []
            session.add(TaskRecord(task_type="PARSE_JD", queue_name="interactive", priority=10,
                payload={"revision_id": entity.id, "passive_match": False},
                idempotency_key=f"CORRECTION_PARSE_JD:{operation_id}"))
            enqueue_sync(session, "jd", entity.jd_id)
            return
        value = _typed_value(entity_type, field_name, value)
        setattr(entity, field_name, value)
        if entity_type == "candidate":
            if field_name == "status":
                entity.workflow_previous_status = None
                refresh_links(session, candidate_id=entity.id)
            else:
                profile_field = {"display_name": "name", "total_years": "total_years",
                                 "highest_degree": "highest_degree"}[field_name]
                profile_value = float(value) if isinstance(value, Decimal) else value
                for revision in session.scalars(select(ResumeRevision).join(ResumeDocument).where(
                    ResumeDocument.candidate_id == entity.id, ResumeRevision.is_current.is_(True),
                )):
                    revision.manual_overrides = {**(revision.manual_overrides or {}), profile_field: profile_value}
                    revision.parsed_data = {**(revision.parsed_data or {}), profile_field: profile_value}
            enqueue_sync(session, "candidate", entity.id)
        elif entity_type == "jd":
            if field_name == "status":
                refresh_links(session, jd_id=entity.id)
            elif field_name in ("title", "company"):
                for revision in session.scalars(select(JdRevision).where(
                    JdRevision.jd_id == entity.id, JdRevision.is_current.is_(True),
                )):
                    revision.parsed_data = {**(revision.parsed_data or {}), field_name: value}
            enqueue_sync(session, "jd", entity.id)


def _typed_value(entity_type: str, field: str, value: Any) -> Any:
    if field == "status":
        allowed = ({"PENDING_REVIEW", "AVAILABLE", "ON_HOLD", "ARCHIVED"} if entity_type == "candidate"
                   else {"DRAFT", "OPEN", "PAUSED", "FILLED", "CANCELLED", "ARCHIVED"})
        if value not in allowed:
            raise ValueError("状态无效")
    if field == "total_years" and value is not None:
        try:
            value = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("工作年限无效") from error
        if not value.is_finite() or not 0 <= value <= 80:
            raise ValueError("工作年限无效")
    if field == "priority":
        try:
            value = int(value)
        except (ValueError, TypeError) as error:
            raise ValueError("优先级无效") from error
    if field in ("display_name", "title", "company") and entity_type != "resume_revision" and value is None:
        raise ValueError("名称不能为空")
    return value


def _lookup(session: Session, entity_type: str, entity_id: str) -> Any:
    from kerui_recruit.db.models import Candidate, Jd, JdRevision, ResumeRevision

    cls_map: dict[str, type] = {
        "candidate": Candidate,
        "jd": Jd,
        "jd_revision": JdRevision,
        "resume_revision": ResumeRevision,
    }
    cls = cls_map.get(entity_type)
    if cls is None:
        raise ValueError(f"Unknown entity type: {entity_type}")
    entity = session.get(cls, entity_id)
    if entity is None:
        raise LookupError(f"{entity_type} not found: {entity_id}")
    return entity


def _validate_field(entity_type: str, field_name: str) -> None:
    if field_name not in _CORRECTABLE_FIELDS.get(entity_type, frozenset()):
        raise ValueError(f"{entity_type}.{field_name} cannot be corrected")
