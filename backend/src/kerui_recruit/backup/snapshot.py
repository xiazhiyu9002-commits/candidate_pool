from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from kerui_recruit.backup.service import _sqlite_copy, _validate_sqlite

DATABASE = 'db/recruit.sqlite3'
REBUILD_MARKER = '.search-rebuild-required'
CHUNK_SIZE = 1024 * 1024


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_tree(source: Path, target: Path) -> None:
    """Snapshot SQLite first, then immutable blobs; indexes are disposable."""
    database = source / DATABASE
    if database.exists():
        _sqlite_copy(database, target / DATABASE)
        _validate_sqlite(target / DATABASE)
    for name in ('blobs', 'config'):
        directory = source / name
        if directory.exists():
            shutil.copytree(directory, target / name)
    (target / REBUILD_MARKER).write_text('1\n', encoding='ascii')
