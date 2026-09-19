from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.correction.service import CorrectionService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CorrectionLog
from kerui_recruit.db.session import create_engine_for


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_apply_correction_updates_field_and_logs(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = CorrectionService(session_factory=session_factory)
    log = service.apply_correction(
        entity_type="candidate",
        entity_id=cid,
        field_name="display_name",
        new_value="张四",
        reason="改名纠正",
    )
    assert log.entity_type == "candidate"
    assert log.entity_id == cid
    assert log.old_value == "张三"
    assert log.new_value == "张四"
    assert log.reverted is False

    with session_factory() as session:
        updated = session.get(Candidate, cid)
        assert updated.display_name == "张四"


def test_undo_correction_restores_field_and_marks_log(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = CorrectionService(session_factory=session_factory)
    log = service.apply_correction(
        entity_type="candidate",
        entity_id=cid,
        field_name="display_name",
        new_value="张四",
        reason="改名纠正",
    )

    undone = service.undo_correction(log.id)
    assert undone.reverted is True
    assert undone.reverted_at is not None

    with session_factory() as session:
        restored = session.get(Candidate, cid)
        assert restored.display_name == "张三"


def test_undo_already_reverted_raises(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = CorrectionService(session_factory=session_factory)
    log = service.apply_correction(
        entity_type="candidate",
        entity_id=cid,
        field_name="display_name",
        new_value="张四",
        reason="改名纠正",
    )
    service.undo_correction(log.id)

    with pytest.raises(ValueError, match="already reverted"):
        service.undo_correction(log.id)


def test_rejects_correction_of_internal_fields(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = CorrectionService(session_factory=session_factory)

    with pytest.raises(ValueError, match="cannot be corrected"):
        service.apply_correction(
            entity_type="candidate",
            entity_id=cid,
            field_name="id",
            new_value="replaced-id",
        )
