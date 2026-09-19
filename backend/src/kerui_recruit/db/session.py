from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event


class UnsupportedSQLiteVersion(RuntimeError):
    code = "E_SQLITE_VERSION_UNSAFE"


def assert_supported_sqlite_version(version: tuple[int, int, int]) -> None:
    if version < (3, 35, 0):
        raise UnsupportedSQLiteVersion(
            f"SQLite {version[0]}.{version[1]}.{version[2]} is unsafe for WAL mode"
        )


def create_engine_for(path: Path) -> Engine:
    assert_supported_sqlite_version(sqlite3.sqlite_version_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine
