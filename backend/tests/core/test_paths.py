from pathlib import Path

import pytest

from kerui_recruit.core.paths import (
    AppPaths,
    UnsafeDataRootError,
    default_data_root,
)


def test_app_paths_create_the_complete_runtime_layout(tmp_path: Path) -> None:
    """Omitting a runtime directory would make a clean installation fail later."""
    paths = AppPaths.from_root(tmp_path / "data")

    paths.ensure()

    assert paths.database == tmp_path / "data" / "db" / "recruit.sqlite3"
    assert paths.search == tmp_path / "data" / "search"
    assert paths.blobs == tmp_path / "data" / "blobs"
    assert {path.name for path in (tmp_path / "data").iterdir()} == {
        "db",
        "search",
        "blobs",
        "exports",
        "backups",
        "logs",
        "temp",
        "config",
    }


def test_default_data_root_uses_local_appdata_on_windows(tmp_path: Path) -> None:
    """Using roaming or home data on Windows risks profile synchronization."""
    root = default_data_root(
        platform_name="Windows",
        environ={"LOCALAPPDATA": str(tmp_path / "Local")},
        home=tmp_path / "Home",
    )

    assert root == tmp_path / "Local" / "KeRuiRecruit"


def test_default_data_root_uses_application_support_on_macos(tmp_path: Path) -> None:
    """Using Documents on macOS risks iCloud synchronization conflicts."""
    root = default_data_root(platform_name="Darwin", environ={}, home=tmp_path)

    assert root == tmp_path / "Library" / "Application Support" / "KeRuiRecruit"


def test_cloud_sync_root_is_rejected(tmp_path: Path) -> None:
    """Accepting a known sync root can corrupt SQLite and index files."""
    with pytest.raises(UnsafeDataRootError) as error:
        AppPaths.from_root(
            tmp_path / "OneDrive" / "RecruitData",
            sync_roots=(tmp_path / "OneDrive",),
        )

    assert error.value.code == "E_CONFIG_UNSAFE_DATA_ROOT"
