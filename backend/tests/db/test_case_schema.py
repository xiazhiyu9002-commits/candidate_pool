from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import (
    Candidate,
    CandidateJobCase,
    Jd,
    StageEvent,
)
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_case_links_candidate_and_jd_with_stage_events(tmp_path: Path) -> None:
    """A case must maintain a stable recruitment thread across stage events."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        candidate = Candidate(display_name="张三")
        jd = Jd(company="A", title="Java")
        session.add_all([candidate, jd])
        session.flush()

        case = CandidateJobCase(candidate=candidate, jd=jd, stage="待联系")
        session.add(case)
        session.flush()

        event = StageEvent(case=case, stage="已推荐", note="推给客户")
        session.add(event)
        session.commit()

        loaded = session.scalars(select(CandidateJobCase)).one()
        assert loaded.candidate.display_name == "张三"
        assert loaded.jd.title == "Java"
        assert loaded.stage == "待联系"
        assert loaded.stage_events[0].stage == "已推荐"
        assert loaded.deleted_at is None