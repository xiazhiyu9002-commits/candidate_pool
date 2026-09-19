from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    suffix: str
    path: Path
    created: bool


class BlobStore:
    def __init__(self, root: Path, temp_root: Path) -> None:
        self.root = root
        self.temp_root = temp_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def put(self, source: BinaryIO, suffix: str) -> StoredBlob:
        normalized_suffix = self._normalize_suffix(suffix)
        descriptor, staged_name = tempfile.mkstemp(prefix="blob-", dir=self.temp_root)
        staged_path = Path(staged_name)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as staged:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    staged.write(chunk)
                staged.flush()
                os.fsync(staged.fileno())

            sha256 = digest.hexdigest()
            target = (
                self.root
                / sha256[:2]
                / sha256[2:4]
                / f"{sha256}{normalized_suffix}"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                staged_path.unlink()
                created = False
            else:
                os.replace(staged_path, target)
                created = True
            return StoredBlob(
                sha256=sha256,
                size_bytes=size_bytes,
                suffix=normalized_suffix,
                path=target,
                created=created,
            )
        finally:
            if staged_path.exists():
                staged_path.unlink()

    def open(self, sha256: str, suffix: str) -> BinaryIO:
        normalized_suffix = self._normalize_suffix(suffix)
        path = self.root / sha256[:2] / sha256[2:4] / f"{sha256}{normalized_suffix}"
        return path.open("rb")

    @staticmethod
    def _normalize_suffix(suffix: str) -> str:
        normalized = suffix.lower()
        return normalized if normalized.startswith(".") else f".{normalized}"
