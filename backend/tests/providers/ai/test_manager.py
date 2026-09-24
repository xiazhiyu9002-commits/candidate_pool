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
from kerui_recruit.providers.ai.probes import (
    _PROBE_JSON_PROMPT,
    AiProbeService,
    probed_roles,
)
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.generation_tasks import AiResumeParser
from kerui_recruit.resumes.validity import check_parsed_resume

# JSON 能力探测要求「业务同形对象」，裸 {"ok": true} 不再放行。
PROBE_JSON_OK = '{"name": "张三", "skills": ["Python"], "total_years": 5}'


def _is_json_probe(body: dict) -> bool:
    """按探测载荷精确区分 JSON 能力探测与文本探测，避免靠字符串猜。"""
    return body["messages"][0]["content"] == _PROBE_JSON_PROMPT


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
            # JSON 能力探测要求「业务同形对象」，裸 {"ok": true} 不再放行。
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
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
async def test_http_client_is_isolated_per_event_loop(tmp_path):
    """跨事件循环复用同一个 httpx 客户端会抛 `Event loop is closed`，必须按循环隔离。

    真机证据（2026-09-22 全量接口探针）：`org.import_parse` 偶发 500
    `E_INTERNAL: Event loop is closed`；探针退出阶段 `ai_manager.close()` 报同一句错。
    根因是同步路径里的 `asyncio.run(...)`（`providers/leads.py:extract`、
    `mail/resume_gate.py`）驱动了**全进程共享**的客户端：短命循环关闭后，连接池里那条
    连接被主循环复用就报这个错。
    """
    manager = _simple_manager(tmp_path, lambda request: httpx.Response(200, json={}))
    # 第一个认领的循环（这里就是测试所在的循环）拿到注入的那个客户端，且可重复取得
    # ——正常路径必须仍复用同一个长连接池。
    assert manager._http_client_for_current_loop() is manager._http_client
    assert manager._http_client_for_current_loop() is manager._http_client

    async def observe():
        return manager._http_client_for_current_loop()

    # 另起一个短命循环（等价于业务里的 `asyncio.run`）来观察：它绝不能拿到主循环的客户端。
    foreign = await asyncio.to_thread(lambda: asyncio.run(observe()))
    assert foreign is not manager._http_client

    # 关停时不能因为「表里有一个属于已关闭循环的客户端」而抛错。
    await manager.close()


@pytest.mark.asyncio
async def test_rate_limit_response_slows_the_generation_pacer(tmp_path):
    """限流反馈必须真的打到生成链路的限速器上。

    `AiProviderManager.generate` 是**所有**生成调用（解析/画像/BD/复核）的唯一入口。
    限速器若没接在这里，8 个 worker 对着 3 RPM 的账号就是互相踩——这是 9.8 的接线证据。
    """
    # 探测阶段必须放行，否则拿不到可用角色、请求会在路由层就变成 E_AI_NO_PROVIDER，
    # 压根走不到限速器（第一版测试就踩了这个坑）。
    throttling = {"on": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "my-custom-model"}]})
        body = json.loads(request.content)
        if not throttling["on"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        assert body["model"] == "my-custom-model"
        # 带上 Retry-After：不含它时熔断会按默认 60 秒冷却，测试要等一分钟。
        return httpx.Response(429, headers={"retry-after": "0.05"},
                              json={"error": {"message": "rate limited"}})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="custom_openai", display_name="自定义",
        api_key=SecretStr("sk"), base_url_override="https://custom.example.com/v1",
        models={ModelRole.FAST_TEXT: "my-custom-model"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert ModelRole.FAST_TEXT in roles
    await manager.update_config(
        AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})])
    )
    throttling["on"] = True

    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT,
                                 ExecutionContext.INTERACTIVE)
    before = manager._pacer.rpm
    with pytest.raises(ProviderError):
        await client.complete_text([{"role": "user", "content": "a"}])
    # 内部消化会重试若干次，每次限流都折半；至少折了第一次。
    assert manager._pacer.rpm <= before / 2


@pytest.mark.asyncio
async def test_a_transient_throttle_is_absorbed_inside_the_generation_link(tmp_path):
    """429 不该直接变成任务失败：链路内部等一轮再试，低配额账号表现为「慢但成功」。

    真机证据（2026-09-22 Kimi 该账号 3 RPM）：429 直接上抛时，任务重试间隔
    1s/5s/30s/2min 全部落在同一个限流窗口内，5 份批量里 4 份最终失败；
    而同一批里唯一撑到第 4 次尝试的那份是成功的——差别只在「有没有等到窗口过去」。
    """
    throttled_once = {"armed": False, "done": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "my-custom-model"}]})
        body = json.loads(request.content)
        if _is_json_probe(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        # 探测阶段的文本探测也走这里，所以限流必须等探测完再"上膛"，否则会被探测吃掉。
        if throttled_once["armed"] and not throttled_once["done"]:
            throttled_once["done"] = True
            # 带上 Retry-After：不含它时熔断会按默认 60 秒冷却，测试要等一分钟。
            return httpx.Response(429, headers={"retry-after": "0.05"},
                                  json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="custom_openai", display_name="自定义",
        api_key=SecretStr("sk"), base_url_override="https://custom.example.com/v1",
        models={ModelRole.FAST_TEXT: "my-custom-model"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    await manager.update_config(
        AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})])
    )
    throttled_once["armed"] = True

    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT,
                                 ExecutionContext.INTERACTIVE)
    before = manager._pacer.rpm
    assert await client.complete_text([{"role": "user", "content": "a"}]) == "ok"
    # 限流那一轮必须留下痕迹（降速），否则我们会失去自适应能力。
    assert manager._pacer.rpm < before


@pytest.mark.asyncio
async def test_successful_generation_does_not_slow_the_pacer(tmp_path):
    """正常成功不能误触发降速——否则限速器会把好供应商也越拖越慢。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "my-custom-model"}]})
        body = json.loads(request.content)
        if _is_json_probe(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    manager = _simple_manager(tmp_path, handler)
    conn = AiConnection(
        connection_id="c1", provider_id="custom_openai", display_name="自定义",
        api_key=SecretStr("sk"), base_url_override="https://custom.example.com/v1",
        models={ModelRole.FAST_TEXT: "my-custom-model"},
    )
    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    await manager.update_config(
        AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})])
    )

    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT,
                                 ExecutionContext.INTERACTIVE)
    before = manager._pacer.rpm
    assert await client.complete_text([{"role": "user", "content": "a"}]) == "ok"
    assert manager._pacer.rpm > before


@pytest.mark.asyncio
async def test_custom_openai_probe_then_route_generates_target(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "my-custom-model"}]})
        body = json.loads(request.content)
        calls.append(body["model"])
        if _is_json_probe(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content-of-my-custom-model"}}]})

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
    assert result == "content-of-my-custom-model"
    assert "my-custom-model" in calls


@pytest.mark.asyncio
async def test_known_vendor_future_model_routes_after_probe(tmp_path):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "future-model"}]})
        body = json.loads(request.content)
        calls.append(body["model"])
        if _is_json_probe(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content-of-future-model"}}]})

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
    assert (await client.complete_text([{"role": "user", "content": "a"}])) == "content-of-future-model"
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
async def test_thinking_only_model_allowed_in_fast_slot(tmp_path):
    """强制思考模型可以填进「快速模型」槽位，并且真的能调用成功（第 9 点需求）。

    智谱的 `glm-5.3` 在目录里只声明 reasoning_text。放开快速槽位前，保存会直接被
    「模型 glm-5.3 不支持角色 fast_text」拒掉；放开后必须同时满足：
    校验通过、探测给出 fast_text 角色、路由真的产出目标、调用返回内容。
    """
    manager = make_manager(tmp_path, AiProviderConfig(connections=[]))
    conn = AiConnection(
        connection_id="c1", provider_id="zhipu", display_name="智谱",
        api_key=SecretStr("sk"),
        models={ModelRole.FAST_TEXT: "glm-5.3"},
    )

    report = await manager.probe(conn)
    roles = probed_roles(report, conn)
    assert roles == frozenset({ModelRole.FAST_TEXT})

    # update_config 内部会跑 validate_connection：这里不抛错即证明目录校验已放行。
    await manager.update_config(
        AiProviderConfig(connections=[conn.model_copy(update={"probed_roles": roles})])
    )
    assert manager.has_role(ModelRole.FAST_TEXT) is True

    client = manager.task_client(
        TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE
    )
    assert (await client.complete_text([{"role": "user", "content": "a"}])) == "content-of-glm-5.3"


@pytest.mark.asyncio
async def test_unprobed_role_does_not_generate_target(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]})
        body = json.loads(request.content)
        model = body["model"]
        if model == "deepseek-v4-pro":
            # reasoning 模型探测失败（模型下架）。
            return httpx.Response(404, json={"error": {"message": "model deepseek-v4-pro not found"}})
        if _is_json_probe(body):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content-of-deepseek-v4-flash"}}]})

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


def _effort_manager(tmp_path: Path, bodies: list) -> AiProviderManager:
    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return _simple_manager(tmp_path, handler)


def _effort_connection(model: str, effort: str | None = None) -> AiConnection:
    return AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"), models={ModelRole.FAST_TEXT: model},
        reasoning_efforts={ModelRole.FAST_TEXT: effort} if effort else {},
        probed_roles=frozenset({ModelRole.FAST_TEXT}),
    )


@pytest.mark.asyncio
async def test_slot_effort_turns_thinking_on_and_reaches_the_request_body(tmp_path):
    """AI 设置里按槽位选的强度必须真的进请求体，并且**同时打开思考**。

    `deepseek-flash` 是可切换思考模型（off+auto）：只发 `reasoning_effort` 而不发
    `thinking: enabled` 的话，阿里那类上游会直接 400
    （`'reasoning_effort' must be 'none' when 'enable_thinking' is false`）。
    """
    bodies: list = []
    manager = _effort_manager(tmp_path, bodies)
    await manager.update_config(AiProviderConfig(
        connections=[_effort_connection("deepseek-flash", "low")]))

    client = manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    await client.complete_text([{"role": "user", "content": "a"}])

    assert bodies[-1]["thinking"] == {"type": "enabled"}
    assert bodies[-1]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_query_parse_style_request_prefers_off_over_the_slot_effort(tmp_path):
    """「关思考优先」：槽位配了最高强度也要关掉思考，且不发强度。

    实测阿里 qwen3.8-flash：关思考 3.3 秒 / 开思考+low 14.2 秒，解析字段基本一致。
    """
    bodies: list = []
    manager = _effort_manager(tmp_path, bodies)
    await manager.update_config(AiProviderConfig(
        connections=[_effort_connection("deepseek-flash", "max")]))

    client = manager.task_client(
        TaskKind.QUERY_PARSE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE,
        reasoning_effort="low", prefer_off=True,
    )
    await client.complete_text([{"role": "user", "content": "a"}])

    assert bodies[-1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in bodies[-1]


@pytest.mark.asyncio
async def test_config_rejects_an_effort_the_model_does_not_declare(tmp_path):
    """档位必须落在模型档案声明的集合内。

    `deepseek-v4-flash` 只声明 `low` / `high`；提交 `max` 若被放行，
    保存成功但 `apply_reasoning` 会因档位不在声明里而静默不发字段——又是「设了却没生效」。
    """
    manager = _effort_manager(tmp_path, [])
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("sk"), models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
        reasoning_efforts={ModelRole.FAST_TEXT: "max"},
        probed_roles=frozenset({ModelRole.FAST_TEXT}),
    )
    with pytest.raises(ValueError):
        await manager.update_config(AiProviderConfig(connections=[conn]))
