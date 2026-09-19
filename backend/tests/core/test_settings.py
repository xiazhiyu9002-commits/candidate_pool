from pathlib import Path

from pydantic import SecretStr

from kerui_recruit.core.settings import Settings


def test_settings_resolve_paths_without_exposing_session_token(tmp_path: Path) -> None:
    """A serialized setting must not leak the desktop session token."""
    settings = Settings(data_root=tmp_path / "data", session_token="top-secret")

    assert settings.paths.database == tmp_path / "data" / "db" / "recruit.sqlite3"
    assert "top-secret" not in str(settings)
    assert settings.session_token.get_secret_value() == "top-secret"


def test_bd_search_enabled_flips_with_tavily_key(tmp_path: Path) -> None:
    offline = Settings(data_root=tmp_path / "data", session_token="top-secret")
    assert offline.bd_search_enabled is False

    enabled = Settings(
        data_root=tmp_path / "data",
        session_token="top-secret",
        tavily_api_key=SecretStr("tvly-xxx"),
    )
    assert enabled.bd_search_enabled is True

