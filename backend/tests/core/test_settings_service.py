import json
from pathlib import Path

from kerui_recruit.core.settings_service import SettingsService
from kerui_recruit.core.settings_store import SettingsStore
from kerui_recruit.encryption.service import EncryptionService


def test_settings_update_and_masked_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = SettingsService(store=store, encryption=encryption)

    service.update(
        {
            "deepseek_api_key": "sk-secret-key-123456",
            "deepseek_base_url": "https://api.deepseek.com",
            "tavily_base_url": "https://api.tavily.com",
        }
    )

    masked = service.get_masked()
    assert masked["deepseek_base_url"] == "https://api.deepseek.com"
    assert masked["deepseek_api_key"] != "sk-secret-key-123456"
    assert "sk-" in masked["deepseek_api_key"]
    assert masked["tavily_base_url"] == "https://api.tavily.com"


def test_secrets_are_encrypted_at_rest(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = SettingsService(store=store, encryption=encryption)

    service.update({"tavily_api_key": "tvly-secret-key"})

    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert raw["tavily_api_key"] != "tvly-secret-key"
    # 解密后应能还原。
    assert encryption.decrypt(raw["tavily_api_key"]) == "tvly-secret-key"


def test_submitting_an_unchanged_mask_does_not_replace_the_secret(tmp_path: Path) -> None:
    """Saving another setting must not turn the displayed mask into the API key."""
    store = SettingsStore(tmp_path / "settings.json")
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = SettingsService(store=store, encryption=encryption)
    service.update({"deepseek_api_key": "sk-original-secret", "deepseek_base_url": "https://old.example.com"})
    masked_key = service.get_masked()["deepseek_api_key"]

    service.update({"deepseek_api_key": masked_key, "deepseek_base_url": "https://new.example.com"})

    raw = store.load()
    assert encryption.decrypt(raw["deepseek_api_key"]) == "sk-original-secret"
    assert raw["deepseek_base_url"] == "https://new.example.com"
