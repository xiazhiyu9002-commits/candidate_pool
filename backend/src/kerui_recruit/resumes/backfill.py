from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import CandidateContact
from kerui_recruit.duplicates.service import normalize_email, normalize_phone
from kerui_recruit.encryption.service import EncryptionService


def backfill_contact_fingerprints(
    session_factory: sessionmaker[Session],
    encryption: EncryptionService,
) -> int:
    """Backfill ``phone_fingerprint`` / ``email_fingerprint`` for existing contacts.

    Idempotent: only touches contacts that still have an encrypted value but a
    missing fingerprint, so it can run safely on every startup.
    """
    updated = 0
    with session_factory() as session:
        contacts = list(
            session.scalars(
                select(CandidateContact).where(
                    or_(
                        CandidateContact.phone_encrypted.is_not(None),
                        CandidateContact.email_encrypted.is_not(None),
                    )
                )
            ).all()
        )
        for contact in contacts:
            changed = False
            if contact.phone_fingerprint is None and contact.phone_encrypted:
                contact.phone_fingerprint = normalize_phone(
                    encryption.decrypt(contact.phone_encrypted)
                )
                changed = True
            if contact.email_fingerprint is None and contact.email_encrypted:
                contact.email_fingerprint = normalize_email(
                    encryption.decrypt(contact.email_encrypted)
                )
                changed = True
            if changed:
                updated += 1
        session.commit()
    return updated
