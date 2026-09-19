from __future__ import annotations

import json
import os
from pathlib import Path


class SettingsStore:
    """Persist runtime settings to a JSON file.

    The file lives under the data directory's ``config`` folder. Values are
    plain JSON; sensitive keys are encrypted by the caller before saving.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if self.path.exists():
            os.replace(self.path, self.path.with_suffix(".bak"))
        os.replace(tmp, self.path)
