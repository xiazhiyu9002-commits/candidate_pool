from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class UnsafeDataRootError(ValueError):
    code = "E_CONFIG_UNSAFE_DATA_ROOT"


def default_data_root(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    system = platform_name or platform.system()
    environment = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    if system == "Windows":
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise UnsafeDataRootError("Windows LOCALAPPDATA is unavailable")
        return Path(local_app_data) / "KeRuiRecruit"
    if system == "Darwin":
        return user_home / "Library" / "Application Support" / "KeRuiRecruit"
    raise UnsafeDataRootError(f"Unsupported operating system: {system}")


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    search: Path
    blobs: Path
    exports: Path
    backups: Path
    logs: Path
    temp: Path
    config: Path

    @classmethod
    def from_root(
        cls,
        root: Path,
        *,
        sync_roots: Sequence[Path] = (),
    ) -> AppPaths:
        resolved = root.expanduser().resolve(strict=False)
        cls._validate_root(resolved, sync_roots)
        return cls(
            root=resolved,
            database=resolved / "db" / "recruit.sqlite3",
            search=resolved / "search",
            blobs=resolved / "blobs",
            exports=resolved / "exports",
            backups=resolved / "backups",
            logs=resolved / "logs",
            temp=resolved / "temp",
            config=resolved / "config",
        )

    @staticmethod
    def _validate_root(root: Path, sync_roots: Sequence[Path]) -> None:
        if str(root).startswith("\\\\"):
            raise UnsafeDataRootError("Network paths are not supported")
        for sync_root in sync_roots:
            resolved_sync_root = sync_root.expanduser().resolve(strict=False)
            if root == resolved_sync_root or root.is_relative_to(resolved_sync_root):
                raise UnsafeDataRootError("Cloud-synchronized paths are not supported")

    def ensure(self) -> None:
        directories = (
            self.database.parent,
            self.search,
            self.blobs,
            self.exports,
            self.backups,
            self.logs,
            self.temp,
            self.config,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
