from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from kerui_recruit.backup.snapshot import snapshot_tree, file_hash, REBUILD_MARKER

_COPY_DIRS = ("db", "search", "blobs", "config")


class MigrationError(RuntimeError):
    """Domain error carrying a stable machine-readable code for the API layer."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class MigrationReport:
    target_root: str
    files_copied: int
    files_verified: int
    candidate_count: int
    ok: bool


class MigrationService:
    """Copy the local data directory to a new location with hash verification.

    The copy is side-effect free on the source; the caller switches the active
    data root after a successful report and restarts the application.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        current_root: Path,
    ) -> None:
        self.session_factory = session_factory
        self.current_root = current_root

    def migrate_to(self, target_root: str) -> MigrationReport:
        source_root = self.current_root.expanduser().resolve()
        target = self._resolve_target(target_root)
        self._validate_target(source_root, target)

        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.staging-{uuid4().hex[:8]}"
        staging.mkdir()

        try:
            if not (source_root / "db" / "recruit.sqlite3").is_file():
                raise MigrationError("E_MIGRATION_EMPTY_SOURCE", "源目录缺少人才库数据库")
            with tempfile.TemporaryDirectory(prefix="kerui-migration-") as temporary:
                snapshot = Path(temporary)
                snapshot_tree(source_root, snapshot)
                source_manifest = self._essential_manifest(snapshot)
                self._copy_essential_dirs(snapshot, staging)
                staging_manifest = self._essential_manifest(staging)
                if staging_manifest != source_manifest:
                    raise MigrationError(
                        "E_MIGRATION_VERIFY_FAILED",
                        "迁移校验失败：复制后的文件数量或哈希与一致性快照不一致",
                    )
            with closing(sqlite3.connect(staging / "db" / "recruit.sqlite3")) as connection:
                candidate_count = connection.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
            self._validate_target(source_root, target)
            self._promote(staging, target)
        except MigrationError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception as error:  # noqa: BLE001 - surface as readable migration error
            shutil.rmtree(staging, ignore_errors=True)
            raise MigrationError(
                "E_MIGRATION_FAILED", f"迁移失败：{error}", status_code=500
            ) from error

        files_copied = len(source_manifest)
        return MigrationReport(
            target_root=str(target),
            files_copied=files_copied,
            files_verified=files_copied,
            candidate_count=candidate_count,
            ok=True,
        )

    @staticmethod
    def _resolve_target(target_root: str) -> Path:
        if not target_root or not target_root.strip():
            raise MigrationError("E_MIGRATION_INVALID_TARGET", "目标路径为空，请选择一个有效目录")
        try:
            target = Path(target_root.strip()).expanduser().resolve()
        except (OSError, ValueError, RuntimeError) as error:
            raise MigrationError("E_MIGRATION_INVALID_TARGET", "目标路径无法解析") from error
        if not str(target).strip():
            raise MigrationError("E_MIGRATION_INVALID_TARGET", "目标路径无效")
        return target

    @staticmethod
    def _validate_target(source_root: Path, target: Path) -> None:
        if target == source_root:
            raise MigrationError(
                "E_MIGRATION_SAME_PATH", "目标目录与当前数据目录相同，请选择其他目录"
            )
        try:
            target_inside_source = target.is_relative_to(source_root)
        except ValueError:
            target_inside_source = False
        if target_inside_source:
            raise MigrationError(
                "E_MIGRATION_TARGET_INSIDE_SOURCE",
                "目标目录不能位于当前数据目录内部，请选择当前数据目录之外的路径",
            )
        try:
            source_inside_target = source_root.is_relative_to(target)
        except ValueError:
            source_inside_target = False
        if source_inside_target:
            raise MigrationError(
                "E_MIGRATION_SOURCE_INSIDE_TARGET",
                "目标目录不能包含当前数据目录，请选择与当前数据目录无嵌套关系的路径",
            )
        if target.exists():
            if not target.is_dir():
                raise MigrationError(
                    "E_MIGRATION_TARGET_NOT_DIR", "目标路径已存在且不是目录"
                )
            if any(target.iterdir()):
                raise MigrationError(
                    "E_MIGRATION_TARGET_NOT_EMPTY",
                    "目标目录非空，请选择一个空目录后再迁移",
                    status_code=409,
                )

    def _copy_essential_dirs(self, source_root: Path, staging: Path) -> None:
        for name in _COPY_DIRS:
            source = source_root / name
            if not source.exists():
                continue
            shutil.copytree(source, staging / name)
        shutil.copy2(source_root / REBUILD_MARKER, staging / REBUILD_MARKER)

    @staticmethod
    def _promote(staging: Path, target: Path) -> None:
        try:
            if target.exists():
                target.rmdir()
            os.replace(staging, target)
        except OSError as error:
            raise MigrationError(
                "E_MIGRATION_PROMOTE_FAILED",
                f"迁移目录提升失败，原目标未受影响：{error}",
                status_code=500,
            ) from error

    @staticmethod
    def _essential_manifest(root: Path) -> dict[str, str]:
        manifest: dict[str, str] = {}
        for name in _COPY_DIRS:
            base = root / name
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file():
                    digest = file_hash(path)
                    manifest[str(path.relative_to(root))] = digest
        if (root / REBUILD_MARKER).is_file():
            manifest[REBUILD_MARKER] = file_hash(root / REBUILD_MARKER)
        return manifest

    def _count_candidates(self) -> int:
        with self.session_factory() as session:
            result = session.execute(text("SELECT COUNT(*) FROM candidate"))
            return int(result.scalar_one())
