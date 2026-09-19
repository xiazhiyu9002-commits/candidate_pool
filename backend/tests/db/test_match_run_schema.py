from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import MatchResult, MatchRun
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_match_run_and_result_snapshot(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        run = MatchRun(trigger="JD_MATCH", query_text="Java 金融")
        session.add(run)
        session.flush()
        session.add(
            MatchResult(
                run=run,
                candidate_id="cand-1",
                jd_revision_id="jd-rev-1",
                total_score=0.95,
                reason="Java 5年 匹配",
            )
        )
        session.commit()

        loaded = session.scalars(select(MatchRun)).one()
        assert loaded.trigger == "JD_MATCH"
        assert loaded.results[0].candidate_id == "cand-1"
        assert loaded.results[0].status == "未处理"