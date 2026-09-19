from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, CandidateContact, Employee, ResumeDocument, ResumeRevision
from kerui_recruit.encryption.service import EncryptionService


class OrgBindingService:
    """Bind a mapping employee to a talent-pool candidate by phone + name.

    Phone is mandatory and authoritative; name is a secondary hint used only to
    disambiguate when several candidates share the same phone.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        encryption: EncryptionService,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption

    def bind(self, *, employee_id: str, phone: str, name: str | None) -> dict:
        normalized_phone = (phone or "").strip()
        if not normalized_phone:
            raise ValueError("电话为必填项")

        with self.session_factory() as session:
            employee = session.get(Employee, employee_id)
            if employee is None:
                raise LookupError(f"Employee not found: {employee_id}")

            candidates: list[tuple[str, str]] = []
            rows = session.execute(
                select(Candidate.id, Candidate.display_name, CandidateContact.phone_encrypted)
                .join(CandidateContact, CandidateContact.candidate_id == Candidate.id)
                .where(CandidateContact.phone_encrypted.isnot(None))
            ).all()
            for candidate_id, display_name, phone_encrypted in rows:
                if phone_encrypted and self.encryption.decrypt(phone_encrypted) == normalized_phone:
                    candidates.append((candidate_id, display_name))

            candidate_id: str | None = None
            candidate_name: str | None = None
            if candidates:
                exact = [c for c in candidates if name and c[1] == name]
                chosen = exact[0] if exact else candidates[0]
                candidate_id, candidate_name = chosen

            current_revision_id: str | None = None
            if candidate_id is not None:
                current_revision_id = session.scalar(
                    select(ResumeRevision.id)
                    .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                    .where(
                        ResumeDocument.candidate_id == candidate_id,
                        ResumeRevision.is_current.is_(True),
                        ResumeRevision.status == "READY",
                    )
                    .order_by(ResumeRevision.created_at.desc(), ResumeRevision.id.desc())
                    .limit(1)
                )

            employee.phone_encrypted = self.encryption.encrypt(normalized_phone)
            employee.candidate_id = candidate_id
            session.commit()

            return {
                "employee_id": employee_id,
                "matched": candidate_id is not None,
                "candidate_id": candidate_id,
                "candidate_name": candidate_name,
                "name_mismatch": candidate_id is not None and name is not None and candidate_name != name,
                "current_revision_id": current_revision_id,
            }
