import asyncio
import json
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.runtime import create_runtime_app
from kerui_recruit.api.ai_settings import _protection_level
from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.probes import _PROBE_JSON_PROMPT

# JSON 能力探测要求「业务同形对象」，裸 {"ok": true} 不再放行。
PROBE_JSON_OK = '{"name": "张三", "skills": ["Python"], "total_years": 5}'


def _is_json_probe(request: httpx.Request) -> bool:
    body = json.loads(request.content)
    return body["messages"][0]["content"] == _PROBE_JSON_PROMPT


def _mock_ai_client() -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-flash"},
                {"id": "deepseek-v4-pro"},
            ]})
        if _is_json_probe(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content"}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _counting_ai_client(calls: list) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
        if _is_json_probe(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content"}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _app(tmp_path):
    return create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_mock_ai_client(),
    )


def _headers():
    return {"X-Kerui-Session": "test"}


def test_catalog_has_nine_providers(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.get("/api/ai/catalog", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert set(p["provider_id"] for p in body["providers"]) == {
        "deepseek", "kimi_open", "kimi_code", "qwen", "qwen_code",
        "zhipu", "zhipu_code", "siliconflow", "custom_openai",
    }
    assert body["default_provider_id"] == "deepseek"


def test_config_never_returns_key(tmp_path):
    client = TestClient(_app(tmp_path))
    put = client.put("/api/ai/config", json={"connections": [{
        "connection_id": None,
        "provider_id": "deepseek",
        "display_name": "DeepSeek",
        "api_key": "sk-test-secret-value",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]}, headers=_headers())
    assert put.status_code == 200
    assert put.json()["protection_level"] == "single"
    assert put.json()["connections"][0]["has_api_key"] is True

    got = client.get("/api/ai/config", headers=_headers())
    body = got.json()
    assert "sk-test-secret-value" not in str(body)
    assert "encrypted_api_key" not in str(body)
    assert body["connections"][0]["masked_api_key"].startswith("sk-t")
    assert "****" in body["connections"][0]["masked_api_key"]


def test_connections_beyond_two_are_saved_and_keep_their_order(tmp_path):
    client = TestClient(_app(tmp_path))
    connections = [
        {"provider_id": "deepseek", "display_name": "a", "api_key": "k1", "models": {"fast_text": "deepseek-v4-flash"}},
        {"provider_id": "qwen", "display_name": "b", "api_key": "k2", "models": {"fast_text": "qwen3.8-flash"}},
        {"provider_id": "zhipu", "display_name": "c", "api_key": "k3", "models": {"fast_text": "glm-4.7-flashx"}},
    ]
    response = client.put("/api/ai/config", json={"connections": connections}, headers=_headers())
    assert response.status_code == 200
    body = response.json()
    # 数量不设上限；返回顺序即用户排定的主备顺序。
    assert [c["display_name"] for c in body["connections"]] == ["a", "b", "c"]
    # 前两个连接能力重叠 → 双服务保护；第三个是候补。
    assert body["protection_level"] == "dual"


def test_reordering_connections_changes_which_two_are_primary(tmp_path):
    client = TestClient(_app(tmp_path))
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [
        {"provider_id": "deepseek", "display_name": "a", "api_key": "k1", "models": {"fast_text": "deepseek-v4-flash"}},
        {"provider_id": "qwen", "display_name": "b", "api_key": "k2", "models": {"fast_text": "qwen3.8-flash"}},
    ]})
    assert first.status_code == 200
    ids = [c["connection_id"] for c in first.json()["connections"]]

    # 用户把第二个连接移到最上面 → 顺序即主备顺序，配置原样回显。
    reordered = client.put("/api/ai/config", headers=_headers(), json={"connections": [
        {"connection_id": ids[1], "provider_id": "qwen", "display_name": "b", "models": {"fast_text": "qwen3.8-flash"}},
        {"connection_id": ids[0], "provider_id": "deepseek", "display_name": "a", "models": {"fast_text": "deepseek-v4-flash"}},
    ]})
    assert reordered.status_code == 200
    assert [c["display_name"] for c in reordered.json()["connections"]] == ["b", "a"]


def test_new_connection_without_key_is_422(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.put("/api/ai/config", json={"connections": [{
        "provider_id": "deepseek",
        "display_name": "a",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]}, headers=_headers())
    assert response.status_code == 422


def test_status_returns_connections(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.get("/api/ai/status", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert "connections" in body
    assert "last_fallback" in body
    assert body["last_fallback"] is None


def test_probe_uses_unsaved_key_without_echoing_it(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "deepseek", "api_key": "temporary-secret",
    })
    assert response.status_code == 200
    assert "temporary-secret" not in response.text
    body = response.json()
    assert set(body) == {"auth", "text", "json", "reasoning", "vision", "models", "role_models", "probe_token"}
    assert set(body["auth"]) == {"ok", "error_code", "suggested_action"}
    assert body["role_models"]["fast_text"] == "deepseek-flash"
    assert body["role_models"]["reasoning_text"] == "deepseek-v4-pro"


def test_auth_required(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/api/ai/config").status_code == 401


def test_legacy_settings_rejects_generation_field_change(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.put("/api/settings", headers=_headers(), json={"deepseek_api_key": "sk-new-secret"})
    assert response.status_code == 422
    assert response.json()["code"] == "E_AI_SETTINGS_MOVED"


def test_legacy_settings_keeps_search_and_mail_fields(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.put("/api/settings", headers=_headers(), json={"tavily_api_key": "tvly-new"})
    assert response.status_code == 200


def _probe_invalid(tmp_path, payload: dict, calls: list):
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )
    client = TestClient(app)
    return client.post("/api/ai/probe", headers=_headers(), json=payload)


def _conn(cid: str, roles: set) -> AiConnection:
    models = {ModelRole.FAST_TEXT: "m"} if ModelRole.FAST_TEXT in roles else {}
    models.update({ModelRole.VISION: "v"} if ModelRole.VISION in roles else {})
    return AiConnection(
        connection_id=cid, provider_id="deepseek", display_name=cid,
        api_key=SecretStr("k"), models=models, probed_roles=frozenset(roles),
    )


def test_protection_level_requires_role_overlap():
    # 两个连接能力无重叠 → 不构成“双服务保护”。
    disjoint = {"c1": frozenset({ModelRole.FAST_TEXT}), "c2": frozenset({ModelRole.VISION})}
    assert _protection_level(disjoint) == "single"
    # 角色重叠 → 双服务保护。
    overlap = {"c1": frozenset({ModelRole.FAST_TEXT}), "c2": frozenset({ModelRole.FAST_TEXT})}
    assert _protection_level(overlap) == "dual"


def test_probe_rejects_preset_base_url_override_without_request(tmp_path):
    calls: list = []
    response = _probe_invalid(tmp_path, {
        "provider_id": "deepseek",
        "api_key": "sk-test",
        "base_url_override": "https://evil.example.com",
    }, calls)
    assert response.status_code == 422
    assert calls == []  # 任何外部请求发生前即被拒绝


def test_probe_rejects_preset_parameter_style_override_without_request(tmp_path):
    calls: list = []
    response = _probe_invalid(tmp_path, {
        "provider_id": "deepseek",
        "api_key": "sk-test",
        "parameter_style": "standard",
    }, calls)
    assert response.status_code == 422
    assert calls == []


def test_probe_rejects_custom_openai_without_https_base_url(tmp_path):
    calls: list = []
    response = _probe_invalid(tmp_path, {
        "provider_id": "custom_openai",
        "api_key": "sk-test",
        "base_url_override": "http://insecure.example.com/v1",
    }, calls)
    assert response.status_code == 422
    assert calls == []


def test_put_config_rejects_preset_base_url_override_without_request(tmp_path):
    calls: list = []
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )
    client = TestClient(app)
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek",
        "display_name": "DeepSeek",
        "api_key": "sk-test",
        "base_url_override": "https://evil.example.com",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert response.status_code == 422
    assert calls == []


def test_put_custom_openai_saves_with_probed_role(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "custom_openai",
        "display_name": "自定义",
        "api_key": "sk-custom",
        "base_url_override": "https://custom.example.com/v1",
        "models": {"fast_text": "my-custom-model"},
    }]})
    assert response.status_code == 200
    body = response.json()
    assert body["connections"][0]["probed_roles"] == ["fast_text"]
    assert body["connections"][0]["base_url_override"] == "https://custom.example.com/v1"
    assert body["protection_level"] == "single"


def test_probe_reuses_saved_key_by_connection_id(tmp_path):
    client = TestClient(_app(tmp_path))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "saved-secret-key",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert put.status_code == 200
    cid = put.json()["connections"][0]["connection_id"]

    # 重新探测不提交 api_key，仅凭 connection_id 复用既有 Key。
    response = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "deepseek", "connection_id": cid,
    })
    assert response.status_code == 200
    assert "saved-secret-key" not in response.text


def test_probe_without_key_and_connection_id_is_422(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.post("/api/ai/probe", headers=_headers(), json={"provider_id": "deepseek"})
    assert response.status_code == 422


def _counting_app(tmp_path, calls):
    return create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )


def test_probe_reuse_key_requires_same_provider(tmp_path):
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert put.status_code == 200
    cid = put.json()["connections"][0]["connection_id"]
    before = len(calls)
    # 用 DeepSeek 的 connection_id 探测 custom_openai → 供应商不一致，422 且零外部请求。
    response = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "custom_openai", "connection_id": cid,
        "base_url_override": "https://custom.example.com/v1",
    })
    assert response.status_code == 422
    assert len(calls) == before


def test_probe_reuse_key_rejects_custom_openai_address_change(tmp_path):
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "custom_openai", "display_name": "a", "api_key": "k1",
        "base_url_override": "https://a.example.com/v1",
        "models": {"fast_text": "my-model"},
    }]})
    assert put.status_code == 200
    cid = put.json()["connections"][0]["connection_id"]
    before = len(calls)
    # 改变目标 origin 但不提交新 Key → 422 且零外部请求。
    response = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "custom_openai", "connection_id": cid,
        "base_url_override": "https://b.example.com/v1",
    })
    assert response.status_code == 422
    assert len(calls) == before


def test_probe_reuse_key_allows_model_only_change(tmp_path):
    client = TestClient(_app(tmp_path))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert put.status_code == 200
    cid = put.json()["connections"][0]["connection_id"]
    # 同供应商、同地址，仅调整模型 → 仍可复用 Key。
    response = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "deepseek", "connection_id": cid,
        "models": {"fast_text": "deepseek-v4-flash"},
    })
    assert response.status_code == 200


def test_put_config_reuse_key_rejects_provider_change(tmp_path):
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert put.status_code == 200
    cid = put.json()["connections"][0]["connection_id"]
    before = len(calls)
    # 复用 Key 但换供应商 → 422 且零外部请求。
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "qwen", "display_name": "a",
        "models": {"fast_text": "qwen3.8-flash"},
    }]})
    assert response.status_code == 422
    assert len(calls) == before


def test_probe_token_dedups_wizard_save(tmp_path):
    calls: list = []
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )
    client = TestClient(app)
    probe = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "deepseek", "api_key": "k1",
    })
    assert probe.status_code == 200
    token = probe.json()["probe_token"]
    before = len(calls)
    # 保存携带有效 probe_token → 复用探测缓存，不再重复请求。
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "probe_token": token, "models": {"fast_text": "deepseek-flash"},
    }]})
    assert put.status_code == 200
    assert len(calls) == before  # 未新增任何上游请求


def test_probe_receipt_rejects_model_swap(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model-a"}]})
        model = json.loads(request.content)["model"]
        if model == "model-b":
            return httpx.Response(404, json={"error": {"message": "model model-b not found"}})
        if _is_json_probe(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content"}}]})

    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app)
    probe = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "custom_openai", "api_key": "k1",
        "base_url_override": "https://custom.example.com/v1",
        "models": {"fast_text": "model-a"},
    })
    assert probe.status_code == 200
    token = probe.json()["probe_token"]
    # 用 model-a 的回执保存 model-b → 回执校验失败，重新探测 model-b（404）→ 422。
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "custom_openai", "display_name": "a", "api_key": "k1",
        "base_url_override": "https://custom.example.com/v1",
        "probe_token": token, "models": {"fast_text": "model-b"},
    }]})
    assert put.status_code == 422


def test_probe_receipt_is_opaque_and_key_free(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.post("/api/ai/probe", headers=_headers(), json={
        "provider_id": "deepseek", "api_key": "secret-key-value",
    })
    assert resp.status_code == 200
    token = resp.json()["probe_token"]
    assert "secret-key-value" not in token
    # 不是旧版确定性哈希 sha256("deepseek|secret-key-value||")。
    import hashlib
    old = hashlib.sha256("deepseek|secret-key-value||".encode()).hexdigest()
    assert token != old
    assert len(token) == 64  # 随机不透明 32 字节十六进制


def test_new_connection_gets_uuid_not_sequential(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert response.status_code == 200
    import uuid
    cid = response.json()["connections"][0]["connection_id"]
    assert cid != "conn-1"
    uuid.UUID(cid)  # 不抛异常即合法 UUID


def test_unchanged_connection_is_not_reprobed(tmp_path):
    calls: list = []
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )
    client = TestClient(app)
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert first.status_code == 200
    cid = first.json()["connections"][0]["connection_id"]
    before = len(calls)
    # 相同 connection_id、不提交 api_key（复用既有 Key）、模型分配不变 → 不重新探测。
    second = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "deepseek", "display_name": "a",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert second.status_code == 200
    assert len(calls) == before


def test_probe_capability_matrix_is_persisted_and_returned(tmp_path):
    """能力矩阵必须随连接落盘并回给前端。

    只显示一个「可用」无法解释「为什么显示可用但解析一直失败」；落盘 auth/text/json/
    reasoning/vision 五项事实后，界面才能指出到底哪一项没过。
    """
    from kerui_recruit.providers.ai.probes import PROBE_SCHEMA_VERSION

    client = TestClient(_app(tmp_path))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert response.status_code == 200
    connection = response.json()["connections"][0]
    assert connection["probe_version"] == PROBE_SCHEMA_VERSION
    # 五项能力都在矩阵里；该 mock 一律回 200，因此全部通过。
    # 「某几项为 False」的分支由 tests/providers/ai/test_probes.py 覆盖。
    assert set(connection["probed_capabilities"]) == {
        "auth", "text", "json", "reasoning", "vision",
    }
    assert all(connection["probed_capabilities"].values())

    # 重新读取配置（走磁盘）也必须带上矩阵与版本。
    reloaded = client.get("/api/ai/config", headers=_headers()).json()["connections"][0]
    assert reloaded["probed_capabilities"] == connection["probed_capabilities"]
    assert reloaded["probe_version"] == PROBE_SCHEMA_VERSION


def test_stale_probe_version_forces_reprobe(tmp_path):
    """探测口径升级后，即使连接配置一字未改也必须重探。

    否则用户会一直看到按旧口径算出的「可用」——JSON 探测从 ``{"ok": true}`` 收紧为
    业务同形对象就是这种口径升级。
    """
    calls: list = []
    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=_counting_ai_client(calls),
    )
    client = TestClient(app)
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert first.status_code == 200
    cid = first.json()["connections"][0]["connection_id"]

    # 模拟「按旧口径保存过」：把落盘的 probe_version 清空。
    manager = app.state.services.ai_manager
    config = manager.config
    stale = config.model_copy(update={
        "connections": [c.model_copy(update={"probe_version": None}) for c in config.connections],
    })
    asyncio.run(manager.update_config(stale))

    before = len(calls)
    second = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "deepseek", "display_name": "a",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})

    assert second.status_code == 200
    assert len(calls) > before  # 口径过期 → 重新探测


def test_add_backup_while_primary_down(tmp_path):
    state = {"deepseek_down": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-v4-flash"}, {"id": "qwen3.8-flash"},
            ]})
        model = json.loads(request.content)["model"]
        if model == "deepseek-v4-flash" and state["deepseek_down"]:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        if _is_json_probe(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "content"}}]})

    app = create_runtime_app(
        Settings(data_root=tmp_path / "data", session_token=SecretStr("test")),
        ai_http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app)
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "主", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert first.status_code == 200
    cid = first.json()["connections"][0]["connection_id"]

    state["deepseek_down"] = True  # 主服务现在 503
    second = client.put("/api/ai/config", headers=_headers(), json={"connections": [
        {"connection_id": cid, "provider_id": "deepseek", "display_name": "主",
         "models": {"fast_text": "deepseek-v4-flash"}},
        {"provider_id": "qwen", "display_name": "备", "api_key": "k2",
         "models": {"fast_text": "qwen3.8-flash"}},
    ]})
    assert second.status_code == 200
    assert second.json()["protection_level"] == "dual"


def test_delete_conn1_keep_conn2_add_new_no_conflict(tmp_path):
    client = TestClient(_app(tmp_path))
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [
        {"provider_id": "deepseek", "display_name": "a", "api_key": "k1", "models": {"fast_text": "deepseek-v4-flash"}},
        {"provider_id": "qwen", "display_name": "b", "api_key": "k2", "models": {"fast_text": "qwen3.8-flash"}},
    ]})
    assert first.status_code == 200
    kept = first.json()["connections"][1]["connection_id"]

    # 删除 conn-1，保留 conn-2，再新增 zhipu：不应产生 ID 冲突。
    second = client.put("/api/ai/config", headers=_headers(), json={"connections": [
        {"connection_id": kept, "provider_id": "qwen", "display_name": "b", "models": {"fast_text": "qwen3.8-flash"}},
        {"provider_id": "zhipu", "display_name": "c", "api_key": "k3", "models": {"fast_text": "glm-4.7-flashx"}},
    ]})
    assert second.status_code == 200
    ids = [c["connection_id"] for c in second.json()["connections"]]
    assert len(set(ids)) == 2
    assert kept in ids


def test_disable_then_enable_reprobes_and_restores_routing(tmp_path):
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert first.status_code == 200
    cid = first.json()["connections"][0]["connection_id"]

    # 停用。
    disabled = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "deepseek", "display_name": "a",
        "enabled": False, "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert disabled.status_code == 200

    # 重新启用：必须真实探测并恢复实际路由。
    before = len(calls)
    enabled = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "deepseek", "display_name": "a",
        "enabled": True, "models": {"fast_text": "deepseek-v4-flash"},
    }]})
    assert enabled.status_code == 200
    assert len(calls) > before  # 重新启用触发了真实探测
    assert enabled.json()["connections"][0]["probed_roles"] == ["fast_text"]


def test_thinking_model_in_fast_text_is_accepted(tmp_path):
    """思考模型可以填进「快速模型」槽位（第 9 点需求）。

    这条断言**刻意反转**了旧行为：以前 deepseek-v4-pro 这类只声明 reasoning_text 的
    模型填进 fast_text 会被保存前 422 拒掉（旧测试名为
    `test_reasoning_model_in_fast_text_is_422`）。现在允许使用者用思考模型换质量，
    代价是变慢，由使用者自行取舍。
    """
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-pro"},
    }]})
    assert response.status_code == 200
    assert calls  # 放行之后会走真实探测
    assert response.json()["connections"][0]["probed_roles"] == ["fast_text"]


def test_unrelated_model_still_rejected_for_vision_slot(tmp_path):
    """放开快速槽位不得顺手放开视觉槽位：纯文本模型填视觉槽仍要 422 且零外部请求。"""
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"vision": "deepseek-v4-pro"},
    }]})
    assert response.status_code == 422
    assert calls == []


def test_200_save_probed_roles_have_targets(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {
            "fast_text": "deepseek-v4-flash",
            "reasoning_text": "deepseek-v4-pro",
            "vision": "deepseek-v4-flash-vision-exp",
        },
    }]})
    assert put.status_code == 200
    manager = app.state.services.ai_manager
    serving = manager.serving_roles()
    for conn in put.json()["connections"]:
        cid = conn["connection_id"]
        for role in conn["probed_roles"]:
            assert role in serving.get(cid, frozenset())


def test_reasoning_efforts_are_saved_and_echoed(tmp_path):
    """界面选的思考强度必须原样保存并回显，否则刷新后下拉框会跳回默认。"""
    client = TestClient(_app(tmp_path))
    put = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-flash"},
        "reasoning_efforts": {"fast_text": "low"},
    }]})
    assert put.status_code == 200
    assert put.json()["connections"][0]["reasoning_efforts"] == {"fast_text": "low"}
    # 重新读取（走磁盘）同样带上，且不改动探测结果。
    reloaded = client.get("/api/ai/config", headers=_headers()).json()["connections"][0]
    assert reloaded["reasoning_efforts"] == {"fast_text": "low"}
    assert reloaded["probed_roles"] == ["fast_text"]


def test_changing_only_reasoning_effort_does_not_reprobe(tmp_path):
    """只改强度不该触发重新探测：探测结果与档位无关，重探只会白等一次真实调用。"""
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    first = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-flash"},
    }]})
    assert first.status_code == 200
    cid = first.json()["connections"][0]["connection_id"]

    before = len(calls)
    second = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "connection_id": cid, "provider_id": "deepseek", "display_name": "a",
        "models": {"fast_text": "deepseek-flash"},
        "reasoning_efforts": {"fast_text": "high"},
    }]})
    assert second.status_code == 200
    assert second.json()["connections"][0]["reasoning_efforts"] == {"fast_text": "high"}
    assert len(calls) == before


def test_unknown_effort_is_422_without_upstream_request(tmp_path):
    """档位不在模型档案声明里 → 保存前 422 且零外部请求。

    `deepseek-v4-flash` 只声明 `low`/`high`；放行 `max` 会让配置显示已设置、
    而 `apply_reasoning` 因为档位不在声明里静默不发字段。
    """
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-v4-flash"},
        "reasoning_efforts": {"fast_text": "max"},
    }]})
    assert response.status_code == 422
    assert calls == []


def test_unknown_effort_role_is_422(tmp_path):
    calls: list = []
    client = TestClient(_counting_app(tmp_path, calls))
    response = client.put("/api/ai/config", headers=_headers(), json={"connections": [{
        "provider_id": "deepseek", "display_name": "a", "api_key": "k1",
        "models": {"fast_text": "deepseek-flash"},
        "reasoning_efforts": {"fast": "low"},
    }]})
    assert response.status_code == 422
    assert response.json()["code"] == "E_AI_INVALID_ROLE"
    assert calls == []
