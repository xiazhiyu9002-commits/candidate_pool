import sqlite3
import time
from contextlib import closing
from pathlib import Path

from sqlalchemy import Engine, func, insert, inspect, select
from sqlalchemy.orm import Session

from kerui_recruit.db.base import Base
from kerui_recruit.db import models as _models
from kerui_recruit.db.upgrades import DEFAULT_UPGRADES, Upgrade

# 当前目标 schema 版本。**加一条 Upgrade 时必须同步改这里**，否则 `migrate` 的
# `version < target_version` 循环根本不会执行，新列不会加上，存量库启动即报
# 「no such column」。`tests/db/test_migrate.py::test_schema_version_matches_last_upgrade`
# 就是守这条的。
SCHEMA_VERSION = 22


def migrate(
    engine: Engine,
    *,
    target_version: int = SCHEMA_VERSION,
    upgrades: tuple[Upgrade, ...] = DEFAULT_UPGRADES,
) -> None:
    # A versioned database must be checked and snapshotted before create_all:
    # create_all can introduce tables even when the existing schema is newer
    # than this application and therefore must not be opened for migration.
    if not inspect(engine).has_table("schema_version"):
        Base.metadata.create_all(engine)
    _apply_schema_version(engine, target_version=target_version, upgrades=upgrades)


def _apply_schema_version(
    engine: Engine,
    *,
    target_version: int,
    upgrades: tuple[Upgrade, ...],
) -> None:
    with Session(engine) as session:
        current_version = session.scalar(select(func.max(_models.SchemaVersion.version)))
        if current_version is None:
            Base.metadata.create_all(engine)
            session.add(_models.SchemaVersion(version=target_version))
            session.commit()
            return
        if current_version > target_version:
            raise RuntimeError(
                f"数据库 schema 版本 {current_version} 高于当前支持的 {target_version}"
            )

    if current_version == target_version:
        Base.metadata.create_all(engine)
        return

    _snapshot_before_upgrade(engine, current_version, target_version)
    registry = {upgrade.from_version: upgrade for upgrade in upgrades}
    with engine.connect() as connection:
        # 某些升级需要重建表（变更外键/可空性）。SQLite 在事务内无法切换
        # foreign_keys，必须在开启事务前关闭，升级结束后恢复。
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # New tables and ALTER statements belong to the same rollback
            # boundary; the snapshot above still contains the original schema.
            Base.metadata.create_all(connection)
            version = current_version
            while version < target_version:
                upgrade = registry.get(version)
                if upgrade is None or upgrade.to_version <= version:
                    raise RuntimeError(f"缺少 schema v{version} 的升级步骤")
                upgrade.apply(connection)
                connection.execute(
                    insert(_models.SchemaVersion).values(version=upgrade.to_version)
                )
                version = upgrade.to_version
            if version != target_version:
                raise RuntimeError(f"schema 升级终止于 v{version}，目标为 v{target_version}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _snapshot_before_upgrade(engine: Engine, from_version: int, to_version: int) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    source = Path(database)
    if not source.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = source.with_name(
        f"{source.stem}.pre-v{from_version}-to-v{to_version}-{stamp}{source.suffix}"
    )
    temporary = target.with_suffix(target.suffix + ".tmp")
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(temporary)) as target_connection:
            source_connection.backup(target_connection)
    temporary.replace(target)
    return target
