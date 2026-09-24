"""v18 → v19：搜索复核结论增加判据依据（basis）列。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for


def _v18_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE search_review (
                id VARCHAR(36) PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                query_key VARCHAR(64) NOT NULL,
                conditions_text TEXT,
                candidate_id VARCHAR(36) NOT NULL,
                revision_id VARCHAR(36),
                verdict VARCHAR(16) NOT NULL DEFAULT 'pending',
                highlights JSON,
                risks JSON,
                failed BOOLEAN NOT NULL DEFAULT 0,
                error VARCHAR(80),
                CONSTRAINT uq_search_review_query_candidate UNIQUE (query_key, candidate_id)
            );
        """)
        db.execute("INSERT INTO schema_version VALUES (18, '2026-01-01')")
        db.execute(
            "INSERT INTO search_review (id, created_at, updated_at, query_key, candidate_id, verdict) "
            "VALUES ('r1', '2026-01-01', '2026-01-01', 'qk', 'c1', 'recommend')"
        )


def test_v18_to_v19_adds_basis_column_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v18_database(path)
    engine = create_engine_for(path)
    migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("search_review")}
    assert "basis" in columns

    with engine.connect() as db:
        row = db.exec_driver_sql(
            "SELECT query_key, candidate_id, verdict, basis FROM search_review WHERE id='r1'"
        ).one()
    assert row == ("qk", "c1", "recommend", None)
