"""v19 → v20：候选人新增「沟通记录」列（不进画像与索引）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for


def _v19_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE candidate (
                id VARCHAR(36) PRIMARY KEY,
                display_name VARCHAR(200),
                status VARCHAR(32),
                total_years NUMERIC(5,1),
                highest_degree VARCHAR(32),
                created_at DATETIME,
                updated_at DATETIME
            );
        """)
        db.execute("INSERT INTO schema_version VALUES (19, '2026-01-01')")
        db.execute(
            "INSERT INTO candidate VALUES ('c1', '张三', 'AVAILABLE', NULL, NULL, "
            "'2026-01-01', '2026-01-01')"
        )


def test_v19_to_v20_adds_communication_note_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v19_database(path)
    engine = create_engine_for(path)
    migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("candidate")}
    assert "communication_note" in columns

    with engine.connect() as db:
        row = db.exec_driver_sql(
            "SELECT display_name, communication_note FROM candidate WHERE id='c1'"
        ).one()
    assert row == ("张三", None)


def test_v19_upgrade_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v19_database(path)
    engine = create_engine_for(path)
    migrate(engine)
    migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("candidate")}
    assert "communication_note" in columns
