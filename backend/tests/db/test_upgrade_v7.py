"""Literal old-schema fixtures, independent of today's ORM metadata."""
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect

from kerui_recruit.db.migrate import SCHEMA_VERSION, migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.upgrades import Upgrade


def _v6_database(path: Path, *, version: int = 6) -> None:
    with sqlite3.connect(path) as db:
        # Only tables affected by v7 plus immutable history are needed. These
        # definitions deliberately cannot inherit new columns from Base.
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE candidate (id VARCHAR(36) PRIMARY KEY, display_name VARCHAR(200) NOT NULL,
                status VARCHAR(32) NOT NULL, total_years NUMERIC(5,1), highest_degree VARCHAR(32),
                deleted_at DATETIME, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE candidate_contact (id VARCHAR(36) PRIMARY KEY, candidate_id VARCHAR(36),
                email_encrypted TEXT, phone_encrypted TEXT, email_confidence FLOAT, phone_confidence FLOAT,
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE resume_revision (id VARCHAR(36) PRIMARY KEY, document_id VARCHAR(36),
                blob_id VARCHAR(36), content_sha256 VARCHAR(64), original_filename VARCHAR(512),
                display_name VARCHAR(512), status VARCHAR(24), is_current BOOLEAN, raw_text TEXT,
                parsed_data JSON, parse_version VARCHAR(100), error_code VARCHAR(80), error_message TEXT,
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE candidate_job_case (id VARCHAR(36) PRIMARY KEY, candidate_id VARCHAR(36),
                jd_id VARCHAR(36), stage VARCHAR(24), template_id VARCHAR(36), note TEXT,
                deleted_at DATETIME, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE case_round (id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36), round_no INTEGER,
                round_name VARCHAR(64), round_type VARCHAR(32), sort_order INTEGER, source VARCHAR(16),
                skipped BOOLEAN, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE stage_event (id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36), stage VARCHAR(24),
                round_no INTEGER, round_name VARCHAR(64), result VARCHAR(16), note TEXT,
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE reminder (id VARCHAR(36) PRIMARY KEY, title VARCHAR(500), note TEXT,
                remind_at DATETIME, dismissed BOOLEAN, dismissed_at DATETIME, created_at DATETIME,
                updated_at DATETIME);
            INSERT INTO candidate(id, display_name, status) VALUES ('old-person', '历史候选人', 'ON_HOLD');
            INSERT INTO resume_revision(id, status, parsed_data) VALUES ('old-resume', 'READY', '{"name":"历史候选人"}');
            INSERT INTO stage_event VALUES ('old-event', 'old-case', '初试', 1, '初试', '推进',
                '历史原始备注', '2026-01-02 03:04:05', '2026-01-02 03:04:05');
            INSERT INTO reminder(id,title,dismissed) VALUES ('old-reminder','原始提醒',0);
        """)
        db.execute("INSERT INTO schema_version VALUES (?, '2026-01-01')", (version,))


def _schema(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def test_v6_snapshot_is_before_any_schema_mutation_and_history_survives(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v6_database(path)
    old_schema = _schema(path)
    with sqlite3.connect(path) as db:
        old_events = db.execute("SELECT * FROM stage_event").fetchall()
    engine = create_engine_for(path)
    migrate(engine)
    inspector = inspect(engine)
    required = {
        "candidate": {"workflow_previous_status"},
        "candidate_contact": {"manual_fields"},
        "resume_revision": {"manual_overrides", "extraction_diagnostics", "review_data"},
        "candidate_job_case": {"template_version", "template_snapshot"},
        "case_round": {"definition_key"},
        "reminder": {"case_id", "paused_by_workflow", "time_basis"},
    }
    for table, names in required.items():
        assert names <= {column["name"] for column in inspector.get_columns(table)}
    assert "index_sync" in inspector.get_table_names()
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT version FROM schema_version ORDER BY version").scalars().all() == list(range(6, SCHEMA_VERSION + 1))
        assert db.exec_driver_sql("SELECT * FROM stage_event").all() == old_events
        assert db.exec_driver_sql("SELECT status FROM candidate").scalar_one() == "ON_HOLD"
        assert db.exec_driver_sql("SELECT paused_by_workflow FROM reminder").scalar_one() == 0
        assert db.exec_driver_sql("SELECT time_basis FROM reminder").scalar_one() == "LEGACY_SHANGHAI"
    snapshots = list(tmp_path.glob(f"recruit.pre-v6-to-v{SCHEMA_VERSION}-*.sqlite3"))
    assert len(snapshots) == 1
    assert _schema(snapshots[0]) == old_schema
    with sqlite3.connect(snapshots[0]) as db:
        assert db.execute("SELECT * FROM stage_event").fetchall() == old_events
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(6,)]
    migrate(engine)
    assert len(list(tmp_path.glob(f"recruit.pre-v6-to-v{SCHEMA_VERSION}-*.sqlite3"))) == 1


def test_future_schema_rejection_does_not_create_new_tables(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    _v6_database(path, version=99)
    before = _schema(path)
    with pytest.raises(RuntimeError, match="高于当前支持"):
        migrate(create_engine_for(path))
    assert _schema(path) == before


def test_failed_v7_upgrade_rolls_back_new_tables_and_old_table_columns(tmp_path: Path) -> None:
    path = tmp_path / "failed.sqlite3"
    _v6_database(path)
    before = _schema(path)
    def failing_upgrade(connection):
        connection.exec_driver_sql("ALTER TABLE candidate ADD COLUMN interrupted TEXT")
        raise RuntimeError("controlled migration failure")
    with pytest.raises(RuntimeError, match="controlled migration failure"):
        migrate(create_engine_for(path), upgrades=(Upgrade(6, 7, failing_upgrade),))
    assert _schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(6,)]
