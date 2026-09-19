from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.soft_delete.service import SoftDeleteService


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_soft_delete_candidate_sets_deleted_at(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = SoftDeleteService(session_factory=session_factory)
    service.soft_delete("candidate", cid)

    with session_factory() as session:
        c = session.get(Candidate, cid)
        assert c.deleted_at is not None


def test_restore_candidate_clears_deleted_at(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = SoftDeleteService(session_factory=session_factory)
    service.soft_delete("candidate", cid)
    service.restore("candidate", cid)

    with session_factory() as session:
        c = session.get(Candidate, cid)
        assert c.deleted_at is None


def test_is_deleted_returns_true_after_soft_delete(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.commit()
        cid = candidate.id

    service = SoftDeleteService(session_factory=session_factory)
    assert service.is_deleted("candidate", cid) is False
    service.soft_delete("candidate", cid)
    assert service.is_deleted("candidate", cid) is True
    service.restore("candidate", cid)
    assert service.is_deleted("candidate", cid) is False


def test_soft_delete_jd_sets_deleted_at(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        jd = Jd(company="某公司", title="Java")
        session.add(jd)
        session.commit()
        jid = jd.id

    service = SoftDeleteService(session_factory=session_factory)
    service.soft_delete("jd", jid)

    with session_factory() as session:
        j = session.get(Jd, jid)
        assert j.deleted_at is not None


def test_list_deleted_returns_soft_deleted_items(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        jd = Jd(company="某公司", title="Java")
        session.add_all([candidate, jd])
        session.commit()
        cid, jid = candidate.id, jd.id

    service = SoftDeleteService(session_factory=session_factory)
    service.soft_delete("candidate", cid)
    service.soft_delete("jd", jid)

    items = service.list_deleted()
    assert len(items) == 2

    by_type = {item["entity_type"]: item for item in items}
    assert by_type["candidate"]["label"] == "张三"
    assert by_type["jd"]["label"] == "某公司 - Java"


def test_purge_expired_removes_only_old_items(session_factory: sessionmaker[Session]) -> None:
    from datetime import datetime, timedelta, timezone

    with session_factory() as session:
        old_candidate = Candidate(display_name="旧候选人")
        old_jd = Jd(company="旧公司", title="旧岗位")
        recent_candidate = Candidate(display_name="新候选人")
        session.add_all([old_candidate, old_jd, recent_candidate])
        session.commit()
        old_cid, old_jid, recent_cid = old_candidate.id, old_jd.id, recent_candidate.id

    service = SoftDeleteService(session_factory=session_factory)
    service.soft_delete("candidate", old_cid)
    service.soft_delete("jd", old_jid)
    service.soft_delete("candidate", recent_cid)

    # Age the old items beyond the 30-day retention window.
    cutoff = datetime.now(timezone.utc) - timedelta(days=31)
    with session_factory() as session, session.begin():
        session.get(Candidate, old_cid).deleted_at = cutoff
        session.get(Jd, old_jid).deleted_at = cutoff

    removed = service.purge_expired(retention_days=30)

    assert removed == 2
    with session_factory() as session:
        assert session.get(Candidate, old_cid) is None
        assert session.get(Jd, old_jid) is None
        assert session.get(Candidate, recent_cid) is not None