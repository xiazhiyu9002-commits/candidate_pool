from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for


def _v17_database(path: Path) -> None:
    """v17 的 candidate_job_case：jd_id NOT NULL + ON DELETE CASCADE（岗位删则流程删）。"""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at DATETIME);
            CREATE TABLE candidate (id VARCHAR(36) PRIMARY KEY, display_name VARCHAR(200),
                status VARCHAR(32), total_years NUMERIC(5,1), highest_degree VARCHAR(32),
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE jd (id VARCHAR(36) PRIMARY KEY, company VARCHAR(200), title VARCHAR(200),
                status VARCHAR(24), priority INTEGER, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE candidate_job_case (
                id VARCHAR(36) PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                candidate_id VARCHAR(36),
                jd_id VARCHAR(36) NOT NULL,
                stage VARCHAR(24) NOT NULL,
                template_id VARCHAR(36),
                template_version INTEGER,
                template_snapshot JSON,
                note TEXT,
                deleted_at DATETIME,
                candidate_name_snapshot VARCHAR(200),
                candidate_phone_snapshot_encrypted TEXT,
                candidate_email_snapshot_encrypted TEXT,
                candidate_profile_snapshot JSON,
                candidate_deleted_at DATETIME,
                CONSTRAINT ck_case_stage CHECK (stage IN ('待评估','已推荐','入职','客户拒绝')),
                FOREIGN KEY(candidate_id) REFERENCES candidate(id) ON DELETE SET NULL,
                FOREIGN KEY(jd_id) REFERENCES jd(id) ON DELETE CASCADE
            );
        """)
        db.execute("INSERT INTO schema_version VALUES (17, '2026-01-01')")
        db.execute("INSERT INTO candidate VALUES ('c1', '张三', 'AVAILABLE', NULL, NULL, '2026-01-01', '2026-01-01')")
        db.execute("INSERT INTO jd VALUES ('j1', '某公司', '工程师', 'OPEN', 0, '2026-01-01', '2026-01-01')")
        db.execute(
            "INSERT INTO candidate_job_case (id, created_at, updated_at, candidate_id, jd_id, stage) "
            "VALUES ('case1', '2026-01-01', '2026-01-01', 'c1', 'j1', '待评估')"
        )


def test_v17_to_v18_rebuilds_case_with_nullable_jd_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v17_database(path)
    engine = create_engine_for(path)
    migrate(engine)

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("candidate_job_case")}
    assert columns["jd_id"]["nullable"] is True
    assert {"jd_title_snapshot", "jd_company_snapshot", "jd_profile_snapshot",
            "jd_deleted_at"} <= set(columns)

    # 存量流程数据在重建后保持不变。
    with engine.connect() as db:
        row = db.exec_driver_sql(
            "SELECT candidate_id, jd_id, stage FROM candidate_job_case WHERE id='case1'"
        ).one()
        assert row == ("c1", "j1", "待评估")

    # 岗位删除后，流程外键被置空（SET NULL），流程行保留——不再级联删除。
    with engine.begin() as db:
        db.exec_driver_sql("DELETE FROM jd WHERE id='j1'")
    with engine.connect() as db:
        case_row = db.exec_driver_sql(
            "SELECT jd_id FROM candidate_job_case WHERE id='case1'"
        ).one()
        assert case_row == (None,)
        assert db.exec_driver_sql("SELECT COUNT(*) FROM candidate_job_case").scalar_one() == 1


def test_v17_upgrade_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "recruit.sqlite3"
    _v17_database(path)
    engine = create_engine_for(path)
    migrate(engine)
    migrate(engine)
    inspector = inspect(engine)
    assert "jd_title_snapshot" in {c["name"] for c in inspector.get_columns("candidate_job_case")}
