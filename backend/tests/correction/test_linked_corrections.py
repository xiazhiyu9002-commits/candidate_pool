from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from kerui_recruit.correction.service import CorrectionService
from kerui_recruit.db.models import (
    Blob, Candidate, CandidateJobCase, CorrectionLog, IndexSyncRecord, Jd,
    JdRevision, Reminder, ResumeDocument, ResumeRevision, TaskRecord,
)
from tests.correction.test_correction import session_factory


@pytest.fixture
def linked(session_factory):
    with session_factory() as session, session.begin():
        candidate = Candidate(display_name="Original", total_years=Decimal("3.0"), highest_degree="BACHELOR")
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="unused")
        revision = ResumeRevision(document=ResumeDocument(candidate=candidate), blob=blob,
            original_filename="unused.pdf", content_sha256="a" * 64, status="READY", is_current=True,
            parsed_data={"name": "Original", "skills": ["Python"], "total_years": 3, "highest_degree": "BACHELOR"})
        jd = Jd(company="Example", title="Engineer", status="OPEN")
        jd_revision = JdRevision(jd=jd, source_text="Original JD", parsed_data={"skills": ["Python"]}, status="READY")
        case = CandidateJobCase(candidate=candidate, jd=jd, stage="已推荐")
        session.add_all([revision, jd_revision, case])
        session.flush()
        reminder = Reminder(title="Follow up", case_id=case.id, remind_at=datetime.now(timezone.utc))
        session.add(reminder)
        session.flush()
        return candidate.id, revision.id, jd.id, jd_revision.id, reminder.id


@pytest.mark.parametrize("field,new_value,key,expected,old", [
    ("display_name", "Corrected", "name", "Corrected", "Original"),
    ("total_years", "6.5", "total_years", 6.5, 3.0),
    ("highest_degree", "MASTER", "highest_degree", "MASTER", "BACHELOR"),
])
def test_profile_apply_and_undo_preserve_manual_projection(session_factory, linked, field, new_value, key, expected, old):
    cid, rid, *_ = linked
    service = CorrectionService(session_factory)
    correction = service.apply_correction(entity_type="candidate", entity_id=cid, field_name=field, new_value=new_value)
    with session_factory() as session:
        revision = session.get(ResumeRevision, rid)
        assert revision.manual_overrides[key] == expected
        assert revision.parsed_data[key] == expected
        job = session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == cid))
        assert job is not None
        version = job.requested_version
    service.undo_correction(correction.id)
    with session_factory() as session:
        revision = session.get(ResumeRevision, rid)
        assert revision.manual_overrides[key] == old
        assert revision.parsed_data[key] == old
        job = session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == cid))
        assert job.requested_version > version
        assert session.get(CorrectionLog, correction.id).reverted


def test_manual_hold_clears_workflow_release_marker_and_refreshes_reminders(session_factory, linked):
    cid, _, _, _, reminder_id = linked
    with session_factory() as session, session.begin():
        candidate = session.get(Candidate, cid)
        candidate.status = "ON_HOLD"
        candidate.workflow_previous_status = "AVAILABLE"
    CorrectionService(session_factory).apply_correction(entity_type="candidate", entity_id=cid,
        field_name="status", new_value="ON_HOLD")
    with session_factory() as session:
        candidate = session.get(Candidate, cid)
        assert candidate.workflow_previous_status is None
        assert candidate.status == "ON_HOLD"
        assert session.get(Reminder, reminder_id).paused_by_workflow is True


def test_jd_close_and_undo_refresh_reminders_and_outbox(session_factory, linked):
    _, _, jid, _, reminder_id = linked
    service = CorrectionService(session_factory)
    correction = service.apply_correction(entity_type="jd", entity_id=jid, field_name="status", new_value="FILLED")
    with session_factory() as session:
        assert session.get(Reminder, reminder_id).paused_by_workflow is True
        assert session.scalar(select(IndexSyncRecord).where(IndexSyncRecord.entity_id == jid)) is not None
    service.undo_correction(correction.id)
    with session_factory() as session:
        assert session.get(Jd, jid).status == "OPEN"
        assert session.get(Reminder, reminder_id).paused_by_workflow is False


def test_raw_jd_correction_and_undo_invalidate_derived_data_and_queue_parse(session_factory, linked):
    _, _, _, rid, _ = linked
    service = CorrectionService(session_factory)
    correction = service.apply_correction(entity_type="jd_revision", entity_id=rid,
        field_name="source_text", new_value="Unparsed replacement")
    with session_factory() as session:
        revision = session.get(JdRevision, rid)
        assert revision.source_text == "Unparsed replacement" and revision.status == "PENDING"
        assert revision.parsed_data is None
        assert session.scalar(select(TaskRecord)).payload["revision_id"] == rid
    service.undo_correction(correction.id)
    with session_factory() as session:
        revision = session.get(JdRevision, rid)
        assert revision.source_text == "Original JD" and revision.status == "PENDING"
        assert revision.parsed_data is None
        assert len(list(session.scalars(select(TaskRecord)))) == 2


def test_processing_raw_jd_edit_rejected_without_audit_write(session_factory, linked):
    _, _, _, rid, _ = linked
    with session_factory() as session, session.begin():
        session.get(JdRevision, rid).status = "PROCESSING"
    with pytest.raises(ValueError, match="解析"):
        CorrectionService(session_factory).apply_correction(entity_type="jd_revision", entity_id=rid,
            field_name="source_text", new_value="Conflict")
    with session_factory() as session:
        assert session.scalar(select(CorrectionLog)) is None
        assert session.get(JdRevision, rid).source_text == "Original JD"


def test_sync_failure_rolls_back_audit_profile_and_manual_overlay(session_factory, linked, monkeypatch):
    import kerui_recruit.correction.service as module
    cid, rid, *_ = linked
    def fail_sync(*_args):
        raise RuntimeError("controlled transaction failure")
    monkeypatch.setattr(module, "enqueue_sync", fail_sync)
    with pytest.raises(RuntimeError, match="transaction failure"):
        CorrectionService(session_factory).apply_correction(entity_type="candidate", entity_id=cid,
            field_name="display_name", new_value="Must roll back")
    with session_factory() as session:
        assert session.get(Candidate, cid).display_name == "Original"
        assert session.get(ResumeRevision, rid).parsed_data["name"] == "Original"
        assert session.get(ResumeRevision, rid).manual_overrides is None
        assert session.scalar(select(CorrectionLog)) is None


@pytest.mark.asyncio
async def test_http_processing_apply_and_undo_return_409_without_mutation(tmp_path):
    from dataclasses import replace
    import httpx
    from kerui_recruit.main import create_app
    from tests.api.test_local_api import build_services
    services, _ = build_services(tmp_path)
    services = replace(services, correction_service=CorrectionService(services.session_factory))
    with services.session_factory() as session, session.begin():
        revision = JdRevision(jd=Jd(title="Role", company="Company"), source_text="Original", status="READY")
        session.add(revision)
        session.flush()
        rid = revision.id
    correction = services.correction_service.apply_correction(entity_type="jd_revision", entity_id=rid,
        field_name="source_text", new_value="Corrected source")
    with services.session_factory() as session, session.begin():
        session.get(JdRevision, rid).status = "PROCESSING"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)), base_url="http://local",
                               headers={"X-Kerui-Session": "test-token"}) as client:
        applied = await client.post("/api/correction/apply", json={"entity_type": "jd_revision", "entity_id": rid,
            "field_name": "source_text", "new_value": "Conflicting source"})
        undone = await client.post(f"/api/correction/{correction.id}/undo")
    assert applied.status_code == undone.status_code == 409
    assert applied.json()["code"] == undone.json()["code"] == "E_CORRECTION_PROCESSING"
    with services.session_factory() as session:
        assert session.get(JdRevision, rid).source_text == "Corrected source"
        assert not session.get(CorrectionLog, correction.id).reverted
