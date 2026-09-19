from __future__ import annotations

import json
import sqlite3

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


class DiagnosticsService:
    """Collect system diagnostics for troubleshooting and support."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        database_path: Path,
    ) -> None:
        self.session_factory = session_factory
        self.database_path = database_path

    def collect(self) -> dict:
        with self.session_factory() as session:
            return {
                "sqlite_version": sqlite3.sqlite_version,
                "database_path": str(self.database_path),
                "database_size_bytes": self.database_path.stat().st_size
                if self.database_path.exists()
                else 0,
                "counts": {
                    "candidates": _count(session, "candidate"),
                    "jd": _count(session, "jd"),
                    "resume_revision": _count(session, "resume_revision"),
                    "match_run": _count(session, "match_run"),
                    "correction_log": _count(session, "correction_log"),
                    "task": _count(session, "task"),
                },
                "pragmas": {
                    "journal_mode": _pragma(session, "journal_mode"),
                    "synchronous": str(_pragma(session, "synchronous")),
                },
            }

    def export_json(self) -> bytes:
        data = self.collect()
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _count(session: Session, table: str) -> int:
    # Table names are validated against a whitelist to prevent SQL injection.
    allowed = {"candidate", "jd", "resume_revision", "match_run", "correction_log", "task"}
    if table not in allowed:
        raise ValueError(f"Unknown table: {table}")
    result = session.execute(text(f"SELECT COUNT(*) FROM {table}"))
    return result.scalar_one()


def _pragma(session: Session, name: str) -> str:
    conn = session.connection()
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar_one()