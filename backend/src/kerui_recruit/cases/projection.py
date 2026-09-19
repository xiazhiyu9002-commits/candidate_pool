"""Read effective business facts without discarding audit history."""
from __future__ import annotations

from datetime import datetime, timezone

from kerui_recruit.db.models import CaseEvent


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def effective_events(events: list[CaseEvent], cutoff: datetime | None = None) -> list[CaseEvent]:
    active = sorted(
        (event for event in events if event.status == "active"
         and (cutoff is None or utc_naive(event.occurred_at) < utc_naive(cutoff))),
        key=lambda event: (utc_naive(event.occurred_at), utc_naive(event.recorded_at), event.id),
    )
    entered = {event.case_round_id for event in active if event.event_type == "INTERVIEW_ENTERED"}
    return [event for event in active if event.event_type != "INTERVIEW_RESULT"
            or event.case_round_id in entered]
