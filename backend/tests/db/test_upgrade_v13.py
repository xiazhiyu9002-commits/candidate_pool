from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for


def _v12_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE jd_revision (id VARCHAR(36) PRIMARY KEY, jd_id VARCHAR(36),
                revision_no INTEGER, source_text TEXT, parsed_data JSON, ai_category VARCHAR(24),
                highest_degree VARCHAR(32), min_years NUMERIC(5,1), location VARCHAR(64),
                status VARCHAR(24), is_current BOOLEAN, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE index_sync (id VARCHAR(36) PRIMARY KEY, entity_type VARCHAR(24),
                entity_id VARCHAR(36), requested_version INTEGER, applied_version INTEGER,
                status VARCHAR(24), attempts INTEGER, last_error TEXT, next_attempt_at DATETIME,
                created_at DATETIME, updated_at DATETIME);
        """)
        db.execute("INSERT INTO schema_version VALUES (12, '2026-01-01')")


def test_v12_to_v13_adds_direction_and_mode_columns(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v12_database(path)
    engine = create_engine_for(path)
    migrate(engine)
    inspector = inspect(engine)
    assert {"review_data", "manual_overrides"} <= {c["name"] for c in inspector.get_columns("jd_revision")}
    assert "requested_mode" in {c["name"] for c in inspector.get_columns("index_sync")}
    with engine.connect() as db:
        versions = db.exec_driver_sql("SELECT version FROM schema_version ORDER BY version").scalars().all()
    assert versions == [12, 13, 14, 15, 16, 17]


def test_v13_upgrade_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v12_database(path)
    engine = create_engine_for(path)
    migrate(engine)
    migrate(engine)
    inspector = inspect(engine)
    assert {"review_data", "manual_overrides"} <= {c["name"] for c in inspector.get_columns("jd_revision")}
    assert "requested_mode" in {c["name"] for c in inspector.get_columns("index_sync")}


def _v13_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE mail_cursor (id VARCHAR(36) PRIMARY KEY, mailbox VARCHAR(200) UNIQUE,
                last_uid INTEGER NOT NULL, created_at DATETIME, updated_at DATETIME);
        """)
        db.execute("INSERT INTO schema_version VALUES (13, '2026-01-01')")


def test_v13_to_v14_adds_mail_cursor_uidvalidity(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v13_database(path)
    engine = create_engine_for(path)
    migrate(engine)
    inspector = inspect(engine)
    assert "uidvalidity" in {c["name"] for c in inspector.get_columns("mail_cursor")}
    with engine.connect() as db:
        versions = db.exec_driver_sql("SELECT version FROM schema_version ORDER BY version").scalars().all()
    assert versions == [13, 14, 15, 16, 17]
