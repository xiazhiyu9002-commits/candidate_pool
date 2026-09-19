import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.config_store import AiConfigStore
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.manager import AiProviderManager
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.factory import LOCAL_VECTOR_DIMENSION, build_providers
from kerui_recruit.providers.local import (
    LocalHashEmbeddingProvider,
    LocalJdParser,
    LocalKeywordReranker,
    LocalResumeParser,
)
from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider


def _close(bundle) -> None:
    if bundle.http_client is not None:
        asyncio.run(bundle.http_client.aclose())


def _settings(**overrides) -> Settings:
    return Settings(
        data_root=Path("/tmp/kerui"),
        session_token=SecretStr("a" * 64),
        **overrides,
    )


class FakeManager:
    def __init__(self, name: str) -> None:
        self.name = name

    def task_client(self, *args, **kwargs):
        return None

    def has_role(self, role) -> bool:
        return role == ModelRole.VISION


def test_no_keys_selects_local_providers() -> None:
    bundle = build_providers(_settings())

    assert isinstance(bundle.parser, LocalResumeParser)
    assert isinstance(bundle.jd_parser, LocalJdParser)
    assert isinstance(bundle.embedding, LocalHashEmbeddingProvider)
    assert isinstance(bundle.reranker, LocalKeywordReranker)
    assert bundle.vector_dimension == LOCAL_VECTOR_DIMENSION
    assert bundle.http_client is None


def test_keys_select_remote_providers_with_1024_dimension() -> None:
    bundle = build_providers(
        _settings(
            deepseek_api_key=SecretStr("ds-key"),
            siliconflow_api_key=SecretStr("sf-key"),
        )
    )

    assert isinstance(bundle.embedding, SiliconFlowEmbeddingProvider)
    assert bundle.vector_dimension == 1024
    assert bundle.http_client is not None
    _close(bundle)


def test_fallback_channel_wired_from_settings() -> None:
    """默认开启 Pro 备份通道；置空即关闭。"""
    default = build_providers(_settings(siliconflow_api_key=SecretStr("sf-key")))
    assert default.embedding.fallback_models == ("Pro/BAAI/bge-m3",)
    assert default.reranker.fallback_models == ("Pro/BAAI/bge-reranker-v2-m3",)
    _close(default)

    disabled = build_providers(
        _settings(
            siliconflow_api_key=SecretStr("sf-key"),
            siliconflow_embedding_fallback_model="",
            siliconflow_reranker_fallback_model="",
        )
    )
    assert disabled.embedding.fallback_models == ()
    assert disabled.reranker.fallback_models == ()
    _close(disabled)


def test_llm_only_keeps_local_search_providers() -> None:
    bundle = build_providers(_settings(deepseek_api_key=SecretStr("ds-key")))

    assert isinstance(bundle.embedding, LocalHashEmbeddingProvider)
    assert bundle.vector_dimension == LOCAL_VECTOR_DIMENSION
    _close(bundle)


def test_generation_change_does_not_change_embedding_or_reranker() -> None:
    settings = _settings(siliconflow_api_key=SecretStr("sf-key"))
    before = build_providers(settings, ai_manager=FakeManager("deepseek"))
    after = build_providers(settings, ai_manager=FakeManager("qwen"))
    assert type(before.embedding) is type(after.embedding)
    assert type(before.reranker) is type(after.reranker)
    assert before.vector_dimension == after.vector_dimension
    _close(before)
    _close(after)


def _vision_manager(tmp_path) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash-vision-exp"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ocr-text"}}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = AiProviderManager(
        config_store=store, catalog_service=catalog, probe_service=AiProbeService(catalog, http_client),
        http_client=http_client, circuit_breaker=CircuitBreaker(),
    )
    return manager


def test_vision_hot_swap_without_restart(tmp_path):
    manager = _vision_manager(tmp_path)
    bundle = build_providers(_settings(), ai_manager=manager)

    # 启动时无视觉路由 → OCR 代理抛 E_OCR_REQUIRED，而非 E_AI_NO_PROVIDER。
    with pytest.raises(ProviderError) as caught:
        asyncio.run(bundle.ocr.extract(b"fake", "x.png"))
    assert caught.value.code == "E_OCR_REQUIRED"

    # 保存视觉连接后，下一次请求立即走新视觉路由（无需重启 runtime）。
    vision_conn = AiConnection(
        connection_id="v1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"), models={ModelRole.VISION: "deepseek-v4-flash-vision-exp"},
        probed_roles=frozenset({ModelRole.VISION}),
    )
    asyncio.run(manager.update_config(AiProviderConfig(connections=[vision_conn])))
    assert manager.has_role(ModelRole.VISION) is True
    assert asyncio.run(bundle.ocr.extract(b"fake", "x.png")) == "ocr-text"

    # 停用视觉连接后，恢复无视觉降级语义。
    asyncio.run(manager.update_config(AiProviderConfig(connections=[vision_conn.model_copy(update={"enabled": False})])))
    assert manager.has_role(ModelRole.VISION) is False
    with pytest.raises(ProviderError) as caught2:
        asyncio.run(bundle.ocr.extract(b"fake", "x.png"))
    assert caught2.value.code == "E_OCR_REQUIRED"
