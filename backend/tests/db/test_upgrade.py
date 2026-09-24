from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.orm import Session

from kerui_recruit.db import models as _models
from kerui_recruit.db.base import Base
from kerui_recruit.db.migrate import SCHEMA_VERSION, migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.upgrades import Upgrade


def _create_version_one_database(path: Path) -> None:
    engine = create_engine_for(path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_models.SchemaVersion(version=1))
        session.add(_models.Candidate(display_name="升级保留样本"))
        session.commit()
    engine.dispose()


def test_migrate_upgrades_v1_to_v8_after_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "recruit.sqlite3"
    _create_version_one_database(database)
    engine = create_engine_for(database)

    migrate(engine)

    with Session(engine) as session:
        versions = list(session.scalars(select(_models.SchemaVersion.version)))
        candidate = session.scalar(select(_models.Candidate))
    indexes = {index["name"] for index in inspect(engine).get_indexes("task")}
    lead_columns = {column["name"] for column in inspect(engine).get_columns("bd_lead")}
    stage_event_columns = {column["name"] for column in inspect(engine).get_columns("stage_event")}
    revision_columns = {column["name"] for column in inspect(engine).get_columns("resume_revision")}
    process_columns = {column["name"] for column in inspect(engine).get_columns("hiring_process")}
    case_columns = {column["name"] for column in inspect(engine).get_columns("candidate_job_case")}
    tables = set(inspect(engine).get_table_names())
    snapshots = list(tmp_path.glob("recruit.pre-v*-to-v*.sqlite3"))
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert candidate is not None and candidate.display_name == "升级保留样本"
    assert "ix_task_status_updated" in indexes
    assert {"confidence", "is_hiring", "session_id", "synthesized_json", "posted_time", "salary_range", "level", "requirements"} <= lead_columns
    assert {"round_no", "round_name", "result"} <= stage_event_columns
    assert {"error_code", "error_message"} <= revision_columns
    assert {"version"} <= process_columns
    assert {"template_id"} <= case_columns
    assert {"case_round", "case_event"} <= tables
    assert len(snapshots) == 1 and snapshots[0].stat().st_size > 0


def test_failed_upgrade_rolls_back_schema_and_version(tmp_path: Path) -> None:
    database = tmp_path / "recruit.sqlite3"
    _create_version_one_database(database)
    engine = create_engine_for(database)

    def failing_upgrade(connection: Connection) -> None:
        connection.execute(text("CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("forced upgrade failure")

    with pytest.raises(RuntimeError, match="forced upgrade failure"):
        migrate(
            engine,
            target_version=2,
            upgrades=(Upgrade(1, 2, failing_upgrade),),
        )

    with Session(engine) as session:
        versions = list(session.scalars(select(_models.SchemaVersion.version)))
    assert versions == [1]
    assert "should_rollback" not in inspect(engine).get_table_names()
