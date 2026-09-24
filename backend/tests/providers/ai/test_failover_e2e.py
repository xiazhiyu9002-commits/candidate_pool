"""端到端故障切换验收：通过真实 OpenAIChatAdapter + httpx MockTransport + Manager。

不访问真实供应商；每个用例走完整的 HTTP 边界 → 适配器 → 路由 → 业务动作。
"""
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
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.search.rewrite import SemanticQueryRewriter


def _connection(connection_id: str, provider_id: str, model: str) -> AiConnection:
    return AiConnection(
        connection_id=connection_id,
        provider_id=provider_id,
        display_name=provider_id,
        api_key=SecretStr("sk-test"),
        models={ModelRole.FAST_TEXT: model},
        probed_roles=frozenset({ModelRole.FAST_TEXT}),
    )


def _manager(tmp_path: Path, handler) -> AiProviderManager:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    catalog = CatalogService(cache_path=tmp_path / "catalog.json")
    store = AiConfigStore(path=tmp_path / "ai-providers.json", encryption=encryption, catalog_service=catalog)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = AiProbeService(catalog, http_client)
    manager = AiProviderManager(
        config_store=store, catalog_service=catalog, probe_service=probe,
        http_client=http_client, circuit_breaker=CircuitBreaker(),
    )
    return manager


def _fast_client(manager: AiProviderManager):
    return manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)


def _handler(deepseek_status: int, qwen_status: int, *, qwen_content: str = "ok") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        status = deepseek_status if model == "deepseek-v4-flash" else qwen_status
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": "err"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": qwen_content if model != "deepseek-v4-flash" else "primary"}}]})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_429_falls_back_to_qwen_with_exactly_two_requests(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        calls.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "qwen-ok"}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    result = await _fast_client(manager).complete_text([{"role": "user", "content": "a"}])
    assert result == "qwen-ok"
    assert calls == ["deepseek-v4-flash", "qwen3.8-flash"]  # 恰好两次上游请求
    assert manager.status().last_fallback is not None


@pytest.mark.asyncio
async def test_b_policy_rejection_does_not_call_backup(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(400, json={"error": {"message": "content policy violation"}})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    with pytest.raises(ProviderError) as caught:
        await _fast_client(manager).complete_text([{"role": "user", "content": "a"}])
    assert caught.value.code == "E_API_POLICY"
    assert calls == ["deepseek-v4-flash"]  # 备用 0 次调用


@pytest.mark.asyncio
async def test_c_both_503_returns_safe_diagnostics(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "busy"}})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    with pytest.raises(ProviderError) as caught:
        await _fast_client(manager).complete_text([{"role": "user", "content": "a"}])
    assert caught.value.code == "E_AI_ALL_PROVIDERS_FAILED"
    assert len(caught.value.details) == 2
    assert set(d["provider_id"] for d in caught.value.details) == {"deepseek", "qwen"}


@pytest.mark.asyncio
async def test_d_cooldown_then_single_recovery(tmp_path):
    state = {"fail": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "deepseek-v4-flash" and state["fail"]:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    # 第一次主服务 429 → 备用完成。
    assert (await _fast_client(manager).complete_text([{"role": "user", "content": "a"}])) == "ok"
    state["fail"] = False
    # 冷却结束后主服务恢复。
    import time
    manager._circuit_breaker._clock = lambda: time.monotonic() + 999
    assert (await _fast_client(manager).complete_text([{"role": "user", "content": "b"}])) == "ok"


@pytest.mark.asyncio
async def test_e_kimi_code_not_used_for_background(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        AiConnection(
            connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
            api_key=SecretStr("sk"), models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
        AiConnection(
            connection_id="c2", provider_id="kimi_code", display_name="Kimi Code",
            api_key=SecretStr("sk"), models={ModelRole.FAST_TEXT: "k3"},
            probed_roles=frozenset({ModelRole.FAST_TEXT}),
        ),
    ]))
    bg = manager.task_client(TaskKind.RESUME_PARSE, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND)
    await bg.complete_text([{"role": "user", "content": "a"}])
    assert "k3" not in calls


@pytest.mark.asyncio
async def test_f_cancel_primary_does_not_call_backup(tmp_path):
    started = asyncio.Event()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "deepseek-v4-flash":
            started.set()
            await asyncio.sleep(10)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    task = asyncio.create_task(_fast_client(manager).complete_text([{"role": "user", "content": "a"}]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "qwen3.8-flash" not in calls


@pytest.mark.asyncio
async def test_g_shared_deadline_does_not_call_backup(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "deepseek-v4-flash":
            await asyncio.sleep(0.1)
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    import time
    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    with pytest.raises(ProviderError) as caught:
        await client.complete_text([{"role": "user", "content": "a"}], deadline_monotonic=time.monotonic() + 0.02)
    assert "qwen3.8-flash" not in calls


@pytest.mark.asyncio
async def test_g_deadline_exhaustion_falls_back_to_original_query(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"semantic_query": "rewritten"}'}}]})

    manager = _manager(tmp_path, handler)
    manager._snapshot = manager._build_snapshot(AiProviderConfig(connections=[
        _connection("c1", "deepseek", "deepseek-v4-flash"),
        _connection("c2", "qwen", "qwen3.8-flash"),
    ]))
    import time
    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    rewriter = SemanticQueryRewriter(client)
    # 共享 deadline 已耗尽：改写器应回退原查询，且主/备均不发起任何远程调用。
    result = await rewriter.rewrite("JS 交易系统", deadline_monotonic=time.monotonic() - 1)
    assert result.query == "JS 交易系统"
    assert result.outcome == "unavailable"
    assert calls == []  # 主服务与备用服务均为 0 次调用
