from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import CorrectionLog
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_correction_log_persists_with_undo_flag(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        log = CorrectionLog(
            entity_type="candidate",
            entity_id="cand-1",
            field_name="display_name",
            old_value="张三",
            new_value="张四",
            reason="改名",
        )
        session.add(log)
        session.commit()

    with make_session(tmp_path / "recruit.sqlite3") as session:
        loaded = session.scalars(select(CorrectionLog)).one()
        assert loaded.entity_type == "candidate"
        assert loaded.entity_id == "cand-1"
        assert loaded.field_name == "display_name"
        assert loaded.old_value == "张三"
        assert loaded.new_value == "张四"
        assert loaded.reason == "改名"
        assert loaded.reverted is False
        assert loaded.reverted_at is None