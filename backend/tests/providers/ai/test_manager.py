from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.config_store import AiConfigStore
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, ReasoningMode, TaskKind
from kerui_recruit.providers.ai.manager import AiProviderManager
from kerui_recruit.providers.ai.probes import AiProbeService, probed_roles
from kerui_recruit.providers.generation_tasks import AiResumeParser
from kerui_recruit.resumes.validity import check_parsed_resume


def connection(connection_id: str, provider_id: str, model: str) -> AiConnection:
    return AiConnection(
        connection_id=connection_id,
        provider_id=provider_id,
        display_name=provider_id,
        api_key=SecretStr("sk-test-secret-value"),
        models={ModelRole.FAST_TEXT: model},
        probed_roles=frozenset({ModelRole.FAST_TEXT}),
    )


def make_manager(tmp_path: Path, config: AiProviderConfig) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        if "response_format" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"content-of-{model}"}}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = AiProbeService(catalog, http_client)
    manager = AiProviderManager(
        config_store=store,
        catalog_service=catalog,
        probe_service=probe,
        http_client=http_client,
        circuit_breaker=CircuitBreaker(),
    )
    return manager


@pytest.mark.asyncio
async def test_saved_config_is_visible_to_next_request_without_runtime_rebuild(tmp_path):
    config = AiProviderConfig(connections=[connection("c1", "deepseek", "deepseek-v4-flash")])
    manager = make_manager(tmp_path, config)
    # 让 manager 使用给定 config（而非空 store）：直接构建快照。
    manager = AiProviderManager(
        config_store=manager._config_store,
        catalog_service=manager._catalog_service,
        probe_service=manager._probe_service,
        http_client=manager._http_client,
        circuit_breaker=manager._circuit_breaker,
    )
    # 通过 update_config 注入首个配置。
    await manager.update_config(config)
    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    assert (await client.complete_text([{"role": "user", "content": "a"}])) == "content-of-deepseek-v4-flash"

    identity_before = manager.cache_identity
    await manager.update_config(AiProviderConfig(connections=[
        connection("c1", "qwen", "qwen3.8-flash"),
        connection("c2", "deepseek", "deepseek-v4-flash"),
    ]))
    assert (await client.complete_text([{"role": "user", "content": "b"}])) == "content-of-qwen3.8-flash"
    assert manager.cache_identity != identity_before


@pytest.mark.asyncio
async def test_cache_identity_is_secret_free(tmp_path):
    manager = make_manager(tmp_path, AiProviderConfig(connections=[connection("c1", "deepseek", "deepseek-v4-flash")]))
    await manager.update_config(AiProviderConfig(connections=[connection("c1", "deepseek", "deepseek-v4-flash")]))
    identity = manager.cache_identity
    assert "sk-test-secret-value" not in identity
    assert "deepseek-v4-flash" in identity


def three_role_connection() -> AiConnection:
    return AiConnection(
        connection_id="c1",
        provider_id="deepseek",
        display_name="DeepSeek",
        api_key=SecretStr("sk-test-secret-value"),
        models={
            ModelRole.FAST_TEXT: "deepseek-v4-flash",
            ModelRole.REASONING_TEXT: "deepseek-v4-pro",
            ModelRole.VISION: "deepseek-v4-flash-vision-exp",
        },
        probed_roles=frozenset({ModelRole.FAST_TEXT, ModelRole.REASONING_TEXT, ModelRole.VISION}),
    )


@pytest.mark.asyncio
async def test_role_routing_uses_correct_model_per_role(tmp_path):
    config = AiProviderConfig(connections=[three_role_connection()])
    manager = make_manager(tmp_path, config)
    await manager.update_config(config)

    fast = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    assert (await fast.complete_text([{"role": "user", "content": "a"}])) == "content-of-deepseek-v4-flash"

    reasoning = manager.task_client(TaskKind.BD_PLAN, ModelRole.REASONING_TEXT, ExecutionContext.INTERACTIVE, reasoning=ReasoningMode.REQUIRED)
    assert (await reasoning.complete_text([{"role": "user", "content": "b"}])) == "content-of-deepseek-v4-pro"

    vision = manager.task_client(TaskKind.VISION_PARSE, ModelRole.VISION, ExecutionContext.BACKGROUND)
    assert (await vision.complete_text([{"role": "user", "content": "c"}])) == "content-of-deepseek-v4-flash-vision-exp"


@pytest.mark.asyncio
async def test_save_probe_then_route_uses_correct_models_per_role(tmp_path):
    """与向导相同的保存流程：探测 → 计算 probed_roles → 保存 → 各角色路由到正确模型。"""
    manager = make_manager(tmp_path, AiProviderConfig(connections=[]))
    conn = AiConnection(
        connection_id="c1",
        provider_id="deepseek",
        display_name="DeepSeek",
        api_key=SecretStr("sk-test"),
        models={
            ModelRole.FAST_TEXT: "deepseek-v4-flash",
            ModelRole.REASONING_TEXT: "deepseek-v4-pro",
            ModelRole.VISION: "deepseek-v4-flash-vision-exp",
        },
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.FAST_TEXT, ModelRole.REASONING_TEXT, ModelRole.VISION})

    conn = conn.model_copy(update={"probed_roles": roles})
    await manager.update_config(AiProviderConfig(connections=[conn]))

    fast = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    assert (await fast.complete_text([{"role": "user", "content": "a"}])) == "content-of-deepseek-v4-flash"

    reasoning = manager.task_client(TaskKind.BD_PLAN, ModelRole.REASONING_TEXT, ExecutionContext.INTERACTIVE, reasoning=ReasoningMode.REQUIRED)
    assert (await reasoning.complete_text([{"role": "user", "content": "b"}])) == "content-of-deepseek-v4-pro"

    vision = manager.task_client(TaskKind.VISION_PARSE, ModelRole.VISION, ExecutionContext.BACKGROUND)
    assert (await vision.complete_text([{"role": "user", "content": "c"}])) == "content-of-deepseek-v4-flash-vision-exp"


def _resume_parse_manager(tmp_path: Path, *, call_counts: dict) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        call_counts[model] = call_counts.get(model, 0) + 1
        if model == "deepseek-v4-flash":
            content = '{"name": "张三"}'
        else:
            content = '{"name": "李四", "skills": ["Python"], "experiences": [{"company": "某公司", "title": "工程师"}]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = AiProbeService(catalog, http_client)
    manager = AiProviderManager(
        config_store=store, catalog_service=catalog, probe_service=probe,
        http_client=http_client, circuit_breaker=CircuitBreaker(),
    )
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        AiConnection(
            connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
            api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
        AiConnection(
            connection_id="c2", provider_id="qwen", display_name="通义千问",
            api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "qwen3.8-flash"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
    ]))
    return manager


@pytest.mark.asyncio
async def test_incomplete_resume_does_not_trigger_backup_text_call(tmp_path):
    call_counts: dict = {}
    manager = _resume_parse_manager(tmp_path, call_counts=call_counts)
    parser = AiResumeParser(manager.task_client(TaskKind.RESUME_PARSE, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND))

    result = await parser.parse_resume("张三\nPython 工程师")

    assert result.name == "张三"
    assert call_counts["deepseek-v4-flash"] == 1
    assert call_counts.get("qwen3.8-flash", 0) == 0  # 业务不完整不触发普通备用文本调用
    validity = check_parsed_resume(result, "张三\nPython 工程师")
    assert validity.ok is False
    assert validity.error_code == "E_PARSE_INCOMPLETE"


def _simple_manager(tmp_path: Path, handler) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = AiProbeService(catalog, http_client)
    return AiProviderManager(
        config_store=store, catalog_service=catalog, probe_service=probe,
        http_client=http_client, circuit_breaker=CircuitBreaker(),
    )


@pytest.mark.asyncio
async def test_custom_openai_probe_then_route_generates_target(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "my-custom-model"}]})
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="custom_openai", display_name="自定义",
        api_key=SecretStr("sk"), base_url_override="https://custom.example.com/v1",
        models={ModelRole.FAST_TEXT: "my-custom-model"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.FAST_TEXT})
    await manager.update_config(AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})]))

    assert manager.has_role(ModelRole.FAST_TEXT) is True
    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    result = await client.complete_text([{"role": "user", "content": "a"}])
    assert result == '{"ok": true}'
    assert "my-custom-model" in calls


@pytest.mark.asyncio
async def test_known_vendor_future_model_routes_after_probe(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "future-model"}]})
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"), models={ModelRole.FAST_TEXT: "future-model"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.FAST_TEXT})
    await manager.update_config(AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})]))

    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    assert (await client.complete_text([{"role": "user", "content": "a"}])) == '{"ok": true}'
    assert calls and calls[-1] == "future-model"


@pytest.mark.asyncio
async def test_concurrent_saves_keep_disk_and_memory_consistent(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    manager = _simple_manager(tmp_path, handler)

    def conn(cid: str) -> AiConnection:
        return AiConnection(
            connection_id=cid, provider_id="deepseek", display_name=cid,
            api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        )

    c1 = AiProviderConfig(connections=[conn("a")])
    c2 = AiProviderConfig(connections=[conn("b")])
    await asyncio.gather(manager.update_config(c1), manager.update_config(c2))
    # 并发保存结束后，磁盘配置与内存快照一致。
    assert manager._config_store.load().connections == manager.config.connections


@pytest.mark.asyncio
async def test_optional_thinking_model_allowed_in_reasoning_slot(tmp_path):
    """可选思考模型（off/auto）应能填入思考槽位，并产出 reasoning_text 目标。"""
    manager = make_manager(tmp_path, AiProviderConfig(connections=[]))
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"),
        models={ModelRole.REASONING_TEXT: "deepseek-flash"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.REASONING_TEXT})
    await manager.update_config(AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})]))
    assert manager.has_role(ModelRole.REASONING_TEXT) is True


@pytest.mark.asyncio
async def test_unprobed_role_does_not_generate_target(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]})
        model = json.loads(request.content)["model"]
        if model == "deepseek-v4-pro":
            # reasoning 模型探测失败（模型下架）。
            return httpx.Response(404, json={"error": {"message": "model deepseek-v4-pro not found"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"),
        models={ModelRole.FAST_TEXT: "deepseek-v4-flash", ModelRole.REASONING_TEXT: "deepseek-v4-pro"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.FAST_TEXT})  # reasoning 探测失败，只有 fast_text 通过
    await manager.update_config(AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})]))
    assert manager.has_role(ModelRole.FAST_TEXT) is True
    assert manager.has_role(ModelRole.REASONING_TEXT) is False
    assert manager.has_role(ModelRole.VISION) is False
