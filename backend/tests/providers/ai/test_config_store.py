from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

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


def test_config_accepts_more_than_two_connections():
    config = AiProviderConfig(connections=[connection("a"), connection("b"), connection("c")])
    assert [item.connection_id for item in config.connections] == ["a", "b", "c"]


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


def test_reasoning_efforts_survive_a_save_load_round_trip(tmp_path):
    """按槽位配置的思考强度必须落盘并读回。

    只存在内存里的话，重启后设置悄悄回到默认——使用者看到的下拉框是空的，
    而实际请求已经发了某个档位，两边对不上。
    """
    from kerui_recruit.providers.ai.contracts import ModelRole

    store = make_store(tmp_path)
    store.save(AiProviderConfig(connections=[AiConnection(
        connection_id="c1",
        provider_id="deepseek",
        display_name="DeepSeek",
        api_key=SecretStr("sk-test"),
        models={ModelRole.FAST_TEXT: "deepseek-flash"},
        reasoning_efforts={ModelRole.FAST_TEXT: "low"},
        probed_roles=frozenset({ModelRole.FAST_TEXT}),
    )]))

    loaded = store.load().connections[0]
    assert loaded.reasoning_efforts == {ModelRole.FAST_TEXT: "low"}
    assert store.public_view().connections[0].reasoning_efforts == {ModelRole.FAST_TEXT: "low"}


def test_save_rejects_an_effort_the_model_does_not_declare(tmp_path):
    """档位不在模型档案声明里 → 保存即失败，不能留下「设了却没生效」的配置。"""
    from kerui_recruit.providers.ai.contracts import ModelRole

    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.save(AiProviderConfig(connections=[AiConnection(
            connection_id="c1",
            provider_id="deepseek",
            display_name="DeepSeek",
            api_key=SecretStr("sk-test"),
            models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
            reasoning_efforts={ModelRole.FAST_TEXT: "max"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        )]))
