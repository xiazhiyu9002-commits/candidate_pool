from pathlib import Path

import pytest

from kerui_recruit.bench.e2e_sidecar import prepare_e2e_data


def test_prepare_e2e_data_only_clears_scoped_directory(tmp_path: Path) -> None:
    target = tmp_path / ".e2e-data"
    target.mkdir()
    (target / "old.sqlite3").write_bytes(b"old")

    prepared = prepare_e2e_data(tmp_path, target)

    assert prepared == target.resolve()
    assert not target.exists()


def test_prepare_e2e_data_rejects_a_directory_outside_working_tree(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scoped"):
        prepare_e2e_data(tmp_path / "workspace", tmp_path / ".e2e-data")
