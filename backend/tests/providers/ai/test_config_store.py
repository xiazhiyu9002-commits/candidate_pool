from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.config_store import AiConfigStore

_LEGACY_SENSITIVE = {"text_api_key", "deepseek_api_key", "siliconflow_api_key", "vision_api_key"}


def make_store(tmp_path: Path) -> AiConfigStore:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    return AiConfigStore(
        path=tmp_path / "ai-providers.json",
        encryption=encryption,
        catalog_service=catalog,
        legacy_settings_path=tmp_path / "settings.json",
    )


def connection(connection_id: str, api_key: str = "sk-test", provider_id: str = "deepseek") -> AiConnection:
    return AiConnection(
        connection_id=connection_id,
        provider_id=provider_id,
        display_name=connection_id,
        api_key=SecretStr(api_key),
    )


def write_legacy_settings(tmp_path: Path, **values) -> None:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    data = {}
    for key, value in values.items():
        if key in _LEGACY_SENSITIVE:
            data[key] = encryption.encrypt(value)
        else:
            data[key] = value
    (tmp_path / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_config_rejects_more_than_two_enabled_connections():
    with pytest.raises(ValidationError):
        AiProviderConfig(connections=[connection("a"), connection("b"), connection("c")])


def test_keys_are_encrypted_and_public_view_is_masked(tmp_path):
    store = make_store(tmp_path)
    store.save(AiProviderConfig(connections=[connection("primary", api_key="sk-secret-value")]))
    raw = (tmp_path / "ai-providers.json").read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    assert store.load().connections[0].api_key.get_secret_value() == "sk-secret-value"
    assert "sk-secret-value" not in store.public_view().model_dump_json()


def test_legacy_text_route_remains_primary_and_deepseek_becomes_backup(tmp_path):
    write_legacy_settings(tmp_path, text_api_key="custom-key", deepseek_api_key="deepseek-key")
    config = make_store(tmp_path).migrate_legacy()
    assert [item.provider_id for item in config.connections] == ["custom_openai", "deepseek"]
    assert make_store(tmp_path).migrate_legacy() == config


def test_legacy_vision_merges_into_matching_key(tmp_path):
    write_legacy_settings(
        tmp_path,
        deepseek_api_key="ds-key",
        deepseek_model="deepseek-v4-flash",
        vision_api_key="ds-key",
        vision_model="deepseek-v4-flash-vision-exp",
    )
    config = make_store(tmp_path).migrate_legacy()
    assert [item.provider_id for item in config.connections] == ["deepseek"]
    from kerui_recruit.providers.ai.contracts import ModelRole

    assert config.connections[0].models.get(ModelRole.VISION) == "deepseek-v4-flash-vision-exp"


def test_migration_does_not_emit_plaintext_key(tmp_path):
    write_legacy_settings(tmp_path, text_api_key="custom-secret")
    config = make_store(tmp_path).migrate_legacy()
    assert config.connections[0].api_key.get_secret_value() == "custom-secret"
    # 迁移结果不落盘，仅内存态；磁盘不写明文。
    assert not (tmp_path / "ai-providers.json").exists()
