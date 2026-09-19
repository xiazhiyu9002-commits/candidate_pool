from __future__ import annotations

import shutil
import sys
from pathlib import Path

from kerui_recruit.sidecar import main as sidecar_main


def prepare_e2e_data(working_directory: Path, requested: Path) -> Path:
    workspace = working_directory.resolve()
    target = requested.resolve()
    if target.parent != workspace or target.name != ".e2e-data":
        raise ValueError("E2E data cleanup must stay scoped to .e2e-data in the working directory")
    if target.exists():
        shutil.rmtree(target)
    return target


def main() -> None:
    try:
        data_root_index = sys.argv.index("--data-root") + 1
        requested = Path(sys.argv[data_root_index])
    except (ValueError, IndexError) as error:
        raise SystemExit("--data-root is required") from error
    prepare_e2e_data(Path.cwd(), requested)
    sidecar_main()


if __name__ == "__main__":
    main()
