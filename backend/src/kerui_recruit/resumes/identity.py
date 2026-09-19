from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.models import Candidate, CandidateContact
from kerui_recruit.duplicates.service import normalize_email, normalize_phone
from kerui_recruit.resumes.extract import is_placeholder_name


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    action: str  # MATCHED / IDENTITY_CONFLICT / POSSIBLE_DUPLICATE / NO_MATCH
    candidate_id: str | None = None
    conflict_candidate_ids: list[str] = field(default_factory=list)
    reason: str | None = None


def _candidate_ids_by(session: Session, column: str, fingerprint: str | None) -> list[str]:
    if not fingerprint:
        return []
    return list(
        session.scalars(
            select(CandidateContact.candidate_id)
            .where(getattr(CandidateContact, column) == fingerprint)
            .distinct()
        ).all()
    )


def resolve_identity(
    session: Session,
    *,
    phone: str | None,
    email: str | None,
    name: str | None,
) -> IdentityResolution:
    """Resolve a parsed resume to an existing candidate by contact fingerprints.

    Only phone/email produce automatic matches. Name-only and multi-field
    signals stay at POSSIBLE_DUPLICATE so nothing is auto-merged without review.
    """
    phone_fp = normalize_phone(phone)
    email_fp = normalize_email(email)

    phone_ids = _candidate_ids_by(session, "phone_fingerprint", phone_fp)
    email_ids = _candidate_ids_by(session, "email_fingerprint", email_fp)

    if len(phone_ids) > 1 or len(email_ids) > 1:
        return IdentityResolution(
            action="IDENTITY_CONFLICT",
            conflict_candidate_ids=sorted(set(phone_ids) | set(email_ids)),
            reason="同一手机号或邮箱匹配到多个候选人",
        )

    phone_match = phone_ids[0] if phone_ids else None
    email_match = email_ids[0] if email_ids else None

    if phone_match and email_match and phone_match != email_match:
        return IdentityResolution(
            action="IDENTITY_CONFLICT",
            conflict_candidate_ids=[phone_match, email_match],
            reason="手机号与邮箱分别指向不同候选人",
        )

    matched = phone_match or email_match
    if matched:
        return IdentityResolution(action="MATCHED", candidate_id=matched)

    # 只有姓名一致：仅提示疑似重复，不自动覆盖。
    # 占位名（未知）不是身份证据，否则所有未识别姓名的候选人会互相牵连。
    if name and not is_placeholder_name(name):
        name_match = session.scalar(
            select(Candidate.id)
            .where(Candidate.display_name == name, Candidate.deleted_at.is_(None))
            .order_by(Candidate.created_at.asc())
            .limit(1)
        )
        if name_match:
            return IdentityResolution(
                action="POSSIBLE_DUPLICATE",
                candidate_id=name_match,
                reason="仅姓名一致，需人工确认",
            )

    return IdentityResolution(action="NO_MATCH")
