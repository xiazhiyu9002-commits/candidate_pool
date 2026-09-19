from pathlib import Path

import importlib.resources

from kerui_recruit.sidecar import parse_runtime_args


def test_sidecar_accepts_only_explicit_desktop_runtime_values(tmp_path: Path) -> None:
    """Changing argument names or silently choosing a public bind address must break packaging."""
    options = parse_runtime_args([
        "--port", "43127",
        "--token", "a" * 64,
        "--data-root", str(tmp_path / "data"),
    ])

    assert options.host == "127.0.0.1"
    assert options.port == 43127
    assert options.token == "a" * 64
    assert options.data_root == tmp_path / "data"


def test_builtin_catalog_resolves_as_packaged_resource() -> None:
    """The frozen sidecar must resolve the built-in catalog without the source tree CWD."""
    raw = importlib.resources.files("kerui_recruit.providers.ai").joinpath("provider_catalog.builtin.json").read_text(encoding="utf-8")
    assert '"deepseek"' in raw
    # The seven provider entries are present and DeepSeek is the default.
    for provider_id in ("deepseek", "kimi_open", "kimi_code", "qwen", "zhipu", "siliconflow", "custom_openai"):
        assert f'"{provider_id}"' in raw
