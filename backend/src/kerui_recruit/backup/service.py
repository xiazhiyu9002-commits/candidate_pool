from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import SCHEMA_VERSION


class BackupService:
    """Snapshot the SQLite database to a timestamped backup file."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        engine: Engine,
        database_path: Path,
        backup_dir: Path,
    ) -> None:
        self.session_factory = session_factory
        self.engine = engine
        self.database_path = database_path
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self, label: str = "") -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_label = label.replace(" ", "_").replace("/", "_") if label else "auto"
        filename = f"backup_{timestamp}_{safe_label}.sqlite3"
        target = self.backup_dir / filename

        self._checkpoint()
        _sqlite_copy(self.database_path, target)
        return target

    def list_snapshots(self) -> list[dict[str, str]]:
        snapshots = []
        for file in sorted(self.backup_dir.glob("backup_*.sqlite3"), reverse=True):
            stat = file.stat()
            snapshots.append(
                {
                    "filename": file.name,
                    "path": str(file),
                    "size_bytes": str(stat.st_size),
                    "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return snapshots

    def restore_snapshot(self, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("备份文件名无效")
        source = self.backup_dir / filename
        if not source.exists():
            raise FileNotFoundError(f"Backup not found: {filename}")
        _validate_sqlite(source)

        # The live process must not replace a database underneath active ORM,
        # task and LanceDB handles.  Take a consistent safety snapshot now and
        # stage an immutable source copy.  Runtime applies it before opening any
        # stores on the next launch.
        safety = self.create_snapshot(label="pre_restore")
        restore_id = uuid4().hex
        staged = self.backup_dir / f"restore-source-{restore_id}.sqlite3"
        _sqlite_copy(source, staged)
        intent = {
            "version": 1,
            "restore_id": restore_id,
            "source": staged.name,
            "requested_from": filename,
            "safety_backup": safety.name,
        }
        marker = self.backup_dir / "pending-restore.json"
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, marker)
        return safety

    def _checkpoint(self) -> None:
        with self.session_factory() as session:
            conn = session.connection()
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            session.commit()

    def prune(self, *, keep_daily: int = 7, keep_weekly: int = 4) -> int:
        """Retain the newest daily snapshots plus one per older ISO week.

        Returns the number of snapshots removed.
        """
        snapshots = sorted(
            self.backup_dir.glob("backup_*.sqlite3"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        kept: set[Path] = set()
        daily_kept = 0
        weekly_seen: set[tuple[int, int]] = set()

        for snapshot in snapshots:
            if daily_kept < keep_daily:
                kept.add(snapshot)
                daily_kept += 1
                continue
            week_key = datetime.fromtimestamp(
                snapshot.stat().st_mtime, tz=timezone.utc
            ).isocalendar()[:2]
            if week_key not in weekly_seen and len(weekly_seen) < keep_weekly:
                weekly_seen.add(week_key)
                kept.add(snapshot)

        removed = 0
        for snapshot in snapshots:
            if snapshot not in kept:
                snapshot.unlink(missing_ok=True)
                removed += 1
        return removed


def apply_pending_restore(*, database_path: Path, search_dir: Path, backup_dir: Path) -> dict | None:
    """Apply a staged restore while no database or index handle is open.

    The marker is deliberately retained until ``finalize_pending_restore`` so
    startup can enqueue every restored entity in the same successful launch.
    Repeating this function after a crash is safe: the staged SQLite snapshot
    is immutable and replacing the target is idempotent.
    """
    marker = backup_dir / "pending-restore.json"
    if not marker.exists():
        return None
    try:
        intent = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(intent, dict):
            raise ValueError("恢复标记损坏")
        restore_id = str(intent.get("restore_id", ""))
        if not restore_id or any(character not in "0123456789abcdef" for character in restore_id):
            raise ValueError("恢复标记损坏")
        source_name = str(intent.get("source", ""))
        if Path(source_name).name != source_name:
            raise ValueError("恢复源文件路径无效")
        source = backup_dir / source_name
        if source.parent.resolve() != backup_dir.resolve() or not source.exists():
            raise ValueError("恢复源文件缺失")
        _validate_sqlite(source)
    except (AttributeError, json.JSONDecodeError, OSError, ValueError):
        # A bad restore request must not brick every subsequent launch. Keep
        # the rejected marker for diagnosis while continuing with the live DB.
        rejected = backup_dir / f"rejected-restore-{uuid4().hex}.json"
        os.replace(marker, rejected)
        return None

    database_path.parent.mkdir(parents=True, exist_ok=True)
    prepared = database_path.with_suffix(database_path.suffix + f".restore-{restore_id}.tmp")
    _sqlite_copy(source, prepared)
    _validate_sqlite(prepared)
    for suffix in ("-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
    os.replace(prepared, database_path)

    archived = search_dir.with_name(f"{search_dir.name}.pre-restore-{restore_id}")
    if search_dir.exists() and not archived.exists():
        os.replace(search_dir, archived)
    search_dir.mkdir(parents=True, exist_ok=True)
    return {**intent, "marker": str(marker), "source_path": str(source), "archived_search": str(archived)}


def finalize_pending_restore(report: dict) -> None:
    Path(str(report["marker"])).unlink(missing_ok=True)
    Path(str(report["source_path"])).unlink(missing_ok=True)


def _sqlite_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)


def _validate_sqlite(path: Path) -> None:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("备份数据库完整性检查失败")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "schema_version" not in tables or "candidate" not in tables:
                raise ValueError("备份文件不是人才库数据库")
            version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if version is None:
                raise ValueError("备份数据库缺少 schema 版本")
            if int(version) > SCHEMA_VERSION:
                raise ValueError(
                    f"备份数据库 schema 版本 {version} 高于当前支持的 {SCHEMA_VERSION}"
                )
    except sqlite3.DatabaseError as error:
        raise ValueError("备份数据库无法读取") from error
