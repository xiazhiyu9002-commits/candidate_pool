"""Transaction-local fan-out for entity visibility and reminder eligibility."""
from sqlalchemy import select
from kerui_recruit.db.models import Candidate, CandidateJobCase, CaseEvent


def refresh_links(session, *, candidate_id=None, jd_id=None, case_id=None):
    # Local import avoids the service/projection dependency cycle.
    from kerui_recruit.cases.service import CaseService
    stmt = select(CandidateJobCase)
    if candidate_id is not None:
        stmt = stmt.where(CandidateJobCase.candidate_id == candidate_id)
    if jd_id is not None:
        stmt = stmt.where(CandidateJobCase.jd_id == jd_id)
    if case_id is not None:
        stmt = stmt.where(CandidateJobCase.id == case_id)
    service = CaseService(None)
    for case in session.scalars(stmt):
        service._sync_candidate_and_reminders(session, case)


def reconcile_legacy_workflow_state(session_factory) -> int:
    """Repair projections that older schema versions did not persist.

    This is safe and idempotent on every startup.  Manual ARCHIVED/PENDING_REVIEW
    states are left untouched; the live fact gate still prevents recommendation.
    """
    from kerui_recruit.search.sync import enqueue_sync

    changed = 0
    with session_factory() as session, session.begin():
        candidate_ids = set(session.scalars(
            select(CandidateJobCase.candidate_id)
            .join(CaseEvent, CaseEvent.case_id == CandidateJobCase.id)
            .where(
                CandidateJobCase.deleted_at.is_(None),
                CaseEvent.status == "active",
                (
                    (CaseEvent.event_type == "ONBOARDED")
                    | ((CaseEvent.event_type == "OFFER") & (CaseEvent.result == "已入职"))
                ),
            )
        ))
        for candidate_id in candidate_ids:
            candidate = session.get(Candidate, candidate_id)
            if candidate is None or candidate.deleted_at is not None or candidate.status != "AVAILABLE":
                continue
            candidate.workflow_previous_status = "AVAILABLE"
            candidate.status = "ON_HOLD"
            enqueue_sync(session, "candidate", candidate.id)
            refresh_links(session, candidate_id=candidate.id)
            changed += 1
    return changed
