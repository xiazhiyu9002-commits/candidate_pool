# Resilient AI Provider Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让没有技术背景的个人猎头只需选择供应商并粘贴 API Key 即可使用 AI，同时通过可签名更新的供应商目录、协议适配和安全回退降低未来 API 或模型变更带来的维护成本。

**Architecture:** 招聘业务代码只依赖稳定的能力接口；目录负责描述供应商端点与模型，适配器负责协议差异，路由器负责按能力选择和回退。AI 连接与路由写入独立的 v2 加密配置，非 Embedding 路由通过不可变快照热切换，Embedding 变更必须先完成索引重建。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、httpx、cryptography/Ed25519、pytest；React 18、TypeScript、Vite、Vitest、Testing Library、Playwright；PyInstaller sidecar。

**Spec:** `docs/superpowers/specs/2026-09-11-resilient-ai-provider-architecture-design.md`

## Global Constraints

- Python 必须保持 `>=3.12,<3.13`，不引入 LiteLLM 或其他会显著增大 sidecar 的统一网关依赖。
- 首批目录覆盖 DeepSeek、硅基流动、阿里云百炼/通义千问、智谱、Moonshot/Kimi、MiniMax、百度千帆、腾讯混元、火山方舟和自定义 OpenAI-compatible 服务。
- 远程目录只能声明代码内枚举的协议，不能下发可执行代码、任意 Header 模板或任意请求模板。
- 远程目录必须经 HTTPS 下载并通过 Ed25519 验签；失败时使用最后验证版本，再回退到内置目录。
- API Key 必须通过现有 `EncryptionService` 加密，任何 GET 响应、日志、异常和测试快照都不得包含明文。
- 故障回退只能使用使用者已保存并启用的连接，每项能力单次业务请求最多两个远程尝试。
- `reasoning_content` 必须丢弃，不记录、不展示，也不能代替最终 `content`。
- 非 Embedding 路由保存并探测成功后立即生效；Embedding 变化必须完成索引重建后才能激活。
- 不修改候选人、JD、组织、招聘流程、任务或向量元数据的数据库 Schema。
- 旧 AI 设置迁移必须幂等，迁移完成后保留旧字段供旧版本回退。
- 自定义 Base URL 生产环境只接受 `https://`；测试和显式开发模式可接受 loopback HTTP。
- 当前目录没有 `.git`。执行每个任务前运行 `Test-Path .git`；若仍为 `False`，不要初始化仓库或伪造提交，把任务编号、测试命令和结果追加到 `.trae/ai-provider-implementation-progress.md`。

---

## File map

### New backend units

- `backend/src/kerui_recruit/providers/ai_contracts.py`：稳定能力、请求、响应、模型档案和生成客户端协议。
- `backend/src/kerui_recruit/providers/catalog_models.py`：目录 Pydantic Schema 与协议白名单。
- `backend/src/kerui_recruit/providers/provider_catalog.builtin.json`：离线可用的首批供应商声明。
- `backend/src/kerui_recruit/providers/catalog.py`：内置目录、签名缓存和远程刷新的加载服务。
- `backend/src/kerui_recruit/providers/ai_config.py`：v2 连接、路由与待激活 Embedding 配置类型。
- `backend/src/kerui_recruit/providers/ai_config_store.py`：原子 JSON 保存、加密连接和旧配置迁移。
- `backend/src/kerui_recruit/providers/normalization.py`：结构化输出解析和供应商响应规范化。
- `backend/src/kerui_recruit/providers/adapters/__init__.py`：适配器工厂导出。
- `backend/src/kerui_recruit/providers/adapters/openai_chat.py`：Chat Completions 适配器。
- `backend/src/kerui_recruit/providers/adapters/openai_responses.py`：Responses 适配器。
- `backend/src/kerui_recruit/providers/adapters/anthropic_messages.py`：Messages 适配器。
- `backend/src/kerui_recruit/providers/discovery.py`：模型发现与缓存。
- `backend/src/kerui_recruit/providers/probes.py`：无个人数据的分能力最小探测。
- `backend/src/kerui_recruit/providers/router.py`：路由选择、错误分类、回退和不可变快照。
- `backend/src/kerui_recruit/providers/manager.py`：配置保存后的验证、热切换和旧客户端关闭。
- `backend/src/kerui_recruit/api/ai_settings.py`：`/api/ai/*` 接口。
- `backend/tests/fixtures/provider_catalog.json`：测试目录。
- `backend/tests/providers/test_catalog.py`、`test_ai_config_store.py`、`test_generation_adapters.py`、`test_discovery.py`、`test_router.py`、`test_manager.py`：后端单元测试。
- `backend/tests/api/test_ai_settings.py`：AI 设置 API 测试。
- `scripts/sign_provider_catalog.py`：发布方离线签名工具。

### Modified backend units

- `backend/src/kerui_recruit/core/settings.py:8`：保留通用/邮件设置；旧 AI 字段仅用于兼容读取。
- `backend/src/kerui_recruit/core/settings_store.py:7`：把保存升级为原子写入并保留 `.bak`。
- `backend/src/kerui_recruit/core/settings_service.py:8-106`：停止让新 AI API 写入旧扁平字段。
- `backend/src/kerui_recruit/sidecar.py:30-107`：启动时触发幂等迁移并读取 v2 AI 配置。
- `backend/src/kerui_recruit/providers/openai_compatible.py:11-79`：变为旧调用方兼容包装，不再独立解析响应。
- `backend/src/kerui_recruit/providers/factory.py:29-132`：按快照构造能力代理，移除供应商硬编码选择。
- `backend/src/kerui_recruit/providers/connectivity.py:24-82`：使用能力探测并保留稳定错误码。
- `backend/src/kerui_recruit/providers/vendors.py`：仅保留旧 `/api/settings/vendors` 的兼容映射。
- `backend/src/kerui_recruit/api/services.py:39-69`：注入 `AiProviderManager`。
- `backend/src/kerui_recruit/api/settings.py:12-71`：旧 AI 字段标记弃用，新写入拒绝 AI 字段。
- `backend/src/kerui_recruit/main.py:11-180`：注册 `ai_settings_router`。
- `backend/src/kerui_recruit/runtime.py:1-494`：业务模块改用路由代理并接入热切换生命周期。
- `backend/tests/providers/test_factory.py`、`test_connectivity.py`、`backend/tests/test_runtime.py`、`backend/tests/test_sidecar.py`：兼容与运行时回归。
- `kerui-recruit-sidecar.spec:1-18`：打包内置目录和由构建环境生成的目录源配置。

### Frontend units

- `desktop/src/ai/types.ts`：目录、连接、模型、探测和路由类型。
- `desktop/src/ai/AiServicesPanel.tsx`：AI 状态总览和连接卡片。
- `desktop/src/ai/ConnectionWizard.tsx`：四步添加/更换连接向导。
- `desktop/src/ai/AdvancedRoutingPanel.tsx`：能力主备路由与 Embedding 激活。
- `desktop/src/api/client.ts:1-1060`：新增 `/api/ai/*` 客户端方法。
- `desktop/src/App.tsx:683-879,1244-1246,2477-2535,3160-3180`：移除扁平 AI 页面状态，接入新服务。
- `desktop/src/pages/SettingsPage.tsx:1-430`：重组为 AI、邮箱提醒、数据备份三个区域。
- `desktop/src/styles.css`：新增连接卡片、步骤条、状态和窄屏样式。
- `desktop/tests/AiServicesPanel.test.tsx`、`ConnectionWizard.test.tsx`、`AdvancedRoutingPanel.test.tsx`：组件测试。
- `desktop/tests/api-client.test.ts`、`desktop/tests/App.test.tsx`：客户端与集成回归。
- `desktop/tests/ai-settings.spec.ts`：桌面端配置主路径 E2E。

---

### Task 1: Freeze capability contracts and catalog schema

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai_contracts.py`
- Create: `backend/src/kerui_recruit/providers/catalog_models.py`
- Create: `backend/src/kerui_recruit/providers/provider_catalog.builtin.json`
- Create: `backend/tests/fixtures/provider_catalog.json`
- Create: `backend/tests/providers/test_catalog.py`

**Interfaces:**
- Consumes: existing `ProviderError` from `kerui_recruit.providers.errors` and Pydantic 2.
- Produces: `Capability`, `ProtocolFamily`, `OutputMode`, `GenerationRequest`, `GenerationResult`, `ModelProfile`, `ProviderCatalog`, `ProviderDefinition`, `load_builtin_catalog() -> ProviderCatalog`.

- [ ] **Step 1: Write the failing catalog and contract tests**

```python
def test_catalog_accepts_only_known_protocols() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["providers"][0]["generation_protocol"] = "remote_python"
    with pytest.raises(ValidationError):
        ProviderCatalog.model_validate(payload)


def test_generation_request_defaults_do_not_force_temperature() -> None:
    request = GenerationRequest(messages=[Message(role="user", content="返回 JSON")])
    assert request.temperature is None
    assert request.output_mode is OutputMode.TEXT
```

- [ ] **Step 2: Run the focused tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_catalog.py -q`

Expected: collection fails because `ai_contracts` and `catalog_models` do not exist.

- [ ] **Step 3: Add the closed enums and stable data types**

```python
class Capability(StrEnum):
    TEXT_GENERATION = "text_generation"
    VISION_GENERATION = "vision_generation"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class ProtocolFamily(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_EMBEDDING = "openai_embedding"
    COHERE_RERANK = "cohere_rerank"


class GenerationClient(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
```

Add `extra="forbid"` to every catalog model. `ProviderDefinition` must allow only `bearer` and `api_key_header` authentication, fixed endpoint suffixes, and the enum protocols above. Validate unique provider IDs and monotonic positive `revision`.

- [ ] **Step 4: Add the built-in catalog and fixture**

Use one entry per Global Constraints provider. Each entry must include a stable provider ID, Chinese label, official console/help URL, default Base URL, supported protocol enum, model-list support flag and conservative recommended models. Do not put API Key examples or user data in either JSON file.

- [ ] **Step 5: Run tests and validate the actual built-in file**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_catalog.py -q`

Expected: all tests pass, including a test that calls `ProviderCatalog.model_validate_json()` on the packaged JSON.

- [ ] **Step 6: Record the task checkpoint**

If `.git` is absent, create `.trae/ai-provider-implementation-progress.md` with `apply_patch` and append: `Task 1 — catalog contracts — py -3.12 -m pytest tests/providers/test_catalog.py -q — PASS`.

---

### Task 2: Load, verify, cache, and package signed catalogs

**Files:**
- Create: `backend/src/kerui_recruit/providers/catalog.py`
- Create: `backend/tests/providers/test_catalog_signature.py`
- Create: `scripts/sign_provider_catalog.py`
- Modify: `backend/src/kerui_recruit/core/settings_store.py:7-31`
- Modify: `kerui-recruit-sidecar.spec:1-18`

**Interfaces:**
- Consumes: `ProviderCatalog` and `load_builtin_catalog()` from Task 1; `Ed25519PublicKey` from cryptography.
- Produces: `CatalogStatus`, `CatalogService.current() -> ProviderCatalog`, `CatalogService.refresh() -> CatalogStatus`, atomic `SettingsStore.save(data: dict) -> None`.

- [ ] **Step 1: Write failing signature and fallback tests**

```python
@pytest.mark.asyncio
async def test_invalid_signature_keeps_last_known_good(tmp_path: Path) -> None:
    service = make_service(tmp_path, response=SIGNED_REVISION_2)
    assert (await service.refresh()).revision == 2
    service.http_client = FakeHttpClient(response=TAMPERED_REVISION_3)
    status = await service.refresh()
    assert status.revision == 2
    assert status.update_error_code == "E_CATALOG_SIGNATURE"


def test_settings_store_replaces_atomically_and_keeps_backup(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "value.json")
    store.save({"revision": 1})
    store.save({"revision": 2})
    assert store.load() == {"revision": 2}
    assert json.loads((tmp_path / "value.json.bak").read_text()) == {"revision": 1}
```

- [ ] **Step 2: Run tests and confirm the expected failures**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_catalog_signature.py -q`

Expected: tests fail because `CatalogService` and atomic backup behavior are absent.

- [ ] **Step 3: Implement exact signed envelope verification**

```python
class SignedCatalogEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload_b64: str
    signature_b64: str


def verify_envelope(raw: bytes, public_key_b64: str) -> ProviderCatalog:
    envelope = SignedCatalogEnvelope.model_validate_json(raw)
    payload = base64.b64decode(envelope.payload_b64, validate=True)
    signature = base64.b64decode(envelope.signature_b64, validate=True)
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
    key.verify(signature, payload)
    return ProviderCatalog.model_validate_json(payload)
```

`CatalogService.refresh()` must use a 5-second connect/read timeout, reject non-HTTPS URLs, reject a revision lower than the active cache, write only after validation, and return stable error codes without raising into application startup.

- [ ] **Step 4: Upgrade `SettingsStore.save` to atomic replace**

Write UTF-8 JSON to a sibling `.tmp`, call `flush()` and `os.fsync()`, copy the prior valid file to `.bak`, then call `os.replace()`. On load failure, try `.bak`; if both fail return `{}`.

- [ ] **Step 5: Add the offline signing CLI and production build gate**

```python
def sign_catalog(payload_path: Path, private_key_path: Path, output_path: Path) -> None:
    payload = payload_path.read_bytes()
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(private_key_path.read_text(encoding="ascii").strip(), validate=True)
    )
    envelope = {
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "signature_b64": base64.b64encode(private_key.sign(payload)).decode("ascii"),
    }
    output_path.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
```

In `kerui-recruit-sidecar.spec`, add the built-in JSON with `collect_data_files('kerui_recruit', includes=['providers/provider_catalog.builtin.json'])`. Generate a build-only `provider_catalog_source.json` from `KERUI_PROVIDER_CATALOG_URL` and `KERUI_PROVIDER_CATALOG_PUBLIC_KEY_B64`; when `KERUI_RELEASE_BUILD=1`, raise `RuntimeError` if either is empty.

- [ ] **Step 6: Run signature, store, and sidecar tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_catalog.py tests/providers/test_catalog_signature.py tests/test_sidecar.py -q`

Expected: all tests pass; tampered, lower-revision and HTTP sources retain the last-known-good catalog.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 2 — signed catalog and atomic store — catalog/signature/sidecar tests — PASS` to `.trae/ai-provider-implementation-progress.md` when Git is absent.

---

### Task 3: Persist encrypted v2 connections and migrate legacy settings

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai_config.py`
- Create: `backend/src/kerui_recruit/providers/ai_config_store.py`
- Create: `backend/tests/providers/test_ai_config_store.py`
- Modify: `backend/src/kerui_recruit/sidecar.py:30-107`
- Modify: `backend/tests/test_sidecar.py`

**Interfaces:**
- Consumes: `SettingsStore`, `EncryptionService`, `Capability`.
- Produces: `AiProviderConfig`, `ProviderConnection`, `RouteTarget`, `CapabilityRoute`, `AiConfigStore.load()`, `AiConfigStore.save()`, `migrate_legacy_ai_settings() -> AiProviderConfig`.

- [ ] **Step 1: Write failing encryption and migration tests**

```python
def test_connection_secret_is_encrypted_and_masked(tmp_path: Path) -> None:
    store = make_ai_store(tmp_path)
    saved = store.create_connection(provider_id="deepseek", display_name="DeepSeek", api_key="sk-secret")
    raw = (tmp_path / "ai-providers.json").read_text(encoding="utf-8")
    assert "sk-secret" not in raw
    assert store.list_masked()[0].has_secret is True
    assert store.get_secret(saved.connection_id) == "sk-secret"


def test_legacy_migration_is_idempotent_and_deduplicates_connection(tmp_path: Path) -> None:
    legacy = {"deepseek_api_key": encrypt("sk-one"), "text_api_key": encrypt("sk-one"),
              "text_base_url": "https://api.deepseek.com", "text_model": "deepseek-chat"}
    first = migrate(legacy)
    second = migrate(legacy)
    assert first == second
    assert len(first.connections) == 1
```

- [ ] **Step 2: Run tests and verify red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_ai_config_store.py tests/test_sidecar.py -q`

Expected: new test module fails to import.

- [ ] **Step 3: Define the v2 model with explicit activation state**

```python
class AiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    connections: list[ProviderConnection] = Field(default_factory=list)
    routes: dict[Capability, CapabilityRoute] = Field(default_factory=dict)
    pending_embedding_route: CapabilityRoute | None = None


class RouteTarget(BaseModel):
    connection_id: UUID
    model_id: str


class CapabilityRoute(BaseModel):
    primary: RouteTarget
    fallbacks: list[RouteTarget] = Field(default_factory=list, max_length=1)
    local_fallback: bool = True
```

Persist encrypted secret separately inside each connection record, but omit it from public response models. Reject duplicate connection IDs and route references to missing or disabled connections.

- [ ] **Step 4: Implement the deterministic legacy mapping**

Map `text_*`, `vision_*`, `embedding_*`, `rerank_*` first. Use legacy DeepSeek defaults for missing text/vision values and SiliconFlow defaults for missing embedding/rerank values. Deduplicate by normalized Base URL plus SHA-256 of the decrypted Key held only in memory. Write v2 first, then return it; never delete legacy fields.

- [ ] **Step 5: Wire migration into sidecar startup**

After constructing `EncryptionService`, call migration only when `ai-providers.json` is absent. Environment-only AI settings may seed an in-memory connection for the current run but must not write the environment Key to disk without an explicit user save.

- [ ] **Step 6: Run focused tests and inspect secret leakage**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_ai_config_store.py tests/test_sidecar.py -q`

Expected: all tests pass.

Run: `rg -n "sk-secret|sk-one" backend/tests/.test-data .trae -g '*.json' -g '*.log'`

Expected: no matches; ignore the source test literals themselves.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 3 — encrypted v2 config and legacy migration — ai_config_store/sidecar tests — PASS` when Git is absent.

---

### Task 4: Implement generation adapters and reasoning-safe normalization

**Files:**
- Create: `backend/src/kerui_recruit/providers/normalization.py`
- Create: `backend/src/kerui_recruit/providers/adapters/__init__.py`
- Create: `backend/src/kerui_recruit/providers/adapters/openai_chat.py`
- Create: `backend/src/kerui_recruit/providers/adapters/openai_responses.py`
- Create: `backend/src/kerui_recruit/providers/adapters/anthropic_messages.py`
- Create: `backend/tests/providers/test_generation_adapters.py`
- Modify: `backend/src/kerui_recruit/providers/openai_compatible.py:11-79`

**Interfaces:**
- Consumes: Task 1 contracts, `httpx.AsyncClient`, `map_http_error()`.
- Produces: `build_generation_client(definition, connection, model, http_client) -> GenerationClient`, `parse_structured_result(content, response_model)` and a legacy `OpenAICompatibleClient` wrapper using the new Chat adapter.

- [ ] **Step 1: Write protocol-specific failing tests**

```python
@pytest.mark.asyncio
async def test_chat_adapter_omits_unsupported_optional_fields() -> None:
    client, transport = make_chat_client(supports_temperature=False, supports_json_schema=False)
    await client.generate(GenerationRequest(messages=[Message(role="user", content="hi")]))
    body = transport.last_json
    assert "temperature" not in body
    assert "reasoning_effort" not in body
    assert "response_format" not in body


@pytest.mark.asyncio
async def test_reasoning_content_is_never_returned_when_final_content_is_empty() -> None:
    client = make_chat_client_with_response({"choices": [{"message": {
        "reasoning_content": "private chain", "content": ""}}]})
    with pytest.raises(ProviderError) as error:
        await client.generate(TEXT_REQUEST)
    assert error.value.code == "E_API_SCHEMA"
    assert "private chain" not in str(error.value)
```

Also cover Responses `output_text`, Anthropic text blocks, JSON Schema → JSON Object → prompted JSON one-step downgrade, request ID capture, timeouts and malformed payloads.

- [ ] **Step 2: Run adapter tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_generation_adapters.py -q`

Expected: imports fail because adapters do not exist.

- [ ] **Step 3: Implement shared request construction rules**

Only include optional fields when both the request asks for them and `ModelProfile` declares support. Use connect timeout 10 seconds and total timeout 300 seconds. Map network, 401/403, 404 model, 429 and 5xx to distinct `ProviderError.code` values while preserving only provider request ID.

- [ ] **Step 4: Implement three response parsers**

```python
def final_chat_content(payload: dict[str, Any]) -> str:
    value = payload["choices"][0]["message"].get("content")
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(code="E_API_SCHEMA", retryable=True,
                            user_message="API 未返回最终答案")
    return value.strip()
```

Responses must concatenate only `output` items whose content type is `output_text`. Anthropic must concatenate only `content` blocks with `type == "text"`. Neither parser may serialize unknown fields into exceptions.

- [ ] **Step 5: Convert the legacy client into a compatibility wrapper**

Keep `complete_text()` and `complete_json()` signatures so existing parsers continue to work during migration. Internally create a `GenerationRequest`, call the Chat adapter, and validate the returned final text. Delete the old direct `choices[0]` parsing path.

- [ ] **Step 6: Run adapter plus existing provider tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_generation_adapters.py tests/providers/test_error_mapping.py tests/providers/test_ocr.py -q`

Expected: all tests pass and no assertion exposes reasoning text.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 4 — generation adapters and normalization — adapter/error/OCR tests — PASS` when Git is absent.

---

### Task 5: Discover models and probe capabilities with actionable errors

**Files:**
- Create: `backend/src/kerui_recruit/providers/discovery.py`
- Create: `backend/src/kerui_recruit/providers/probes.py`
- Create: `backend/tests/providers/test_discovery.py`
- Create: `backend/tests/providers/test_probes.py`
- Modify: `backend/src/kerui_recruit/providers/connectivity.py:24-82`
- Modify: `backend/tests/providers/test_connectivity.py`

**Interfaces:**
- Consumes: catalog definitions, decrypted connection view, generation adapters, existing embedding/rerank providers.
- Produces: `DiscoveredModel`, `DiscoveryResult`, `ModelDiscoveryService.discover(connection_id)`, `ProbeResult`, `CapabilityProbeService.probe(connection_id, capabilities)`.

- [ ] **Step 1: Write failing discovery fallback and error-copy tests**

```python
@pytest.mark.asyncio
async def test_discovery_falls_back_to_catalog_models_when_endpoint_is_forbidden() -> None:
    result = await service_with_status(403).discover(CONNECTION_ID)
    assert result.source == "catalog"
    assert result.models[0].id == "deepseek-chat"
    assert result.warning_code == "E_MODEL_LIST_FORBIDDEN"


@pytest.mark.parametrize((status, code, action), [
    (401, "E_API_KEY_INVALID", "重新粘贴 API Key"),
    (402, "E_API_BALANCE", "检查余额或套餐"),
    (429, "E_API_RATE_LIMIT", "稍后重试或配置备用服务"),
])
def test_probe_maps_actionable_messages(status: int, code: str, action: str) -> None:
    result = probe_result_for_status(status)
    assert result.error_code == code
    assert result.suggested_action == action
```

- [ ] **Step 2: Run focused tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_discovery.py tests/providers/test_probes.py -q`

Expected: modules are missing.

- [ ] **Step 3: Implement discovery with a 24-hour cache**

Normalize `/models` responses into ID, display name and catalog-hinted capabilities. Persist only IDs, capability hints, timestamp and connection ID in `provider-probes.json`; never persist tokens or raw response headers. If discovery fails, return catalog recommendations with a warning instead of blocking the wizard.

- [ ] **Step 4: Implement fixed privacy-safe probes**

Use these inputs exactly: text `只返回“连接成功”四个字。`, structured JSON `{"ok": true}`, vision a generated 1×1 white PNG with the prompt `只回答“白色”`, embedding `连接测试`, and rerank query/document both `连接测试`. Cap generation output at 32 tokens when the protocol supports it.

- [ ] **Step 5: Replace connectivity's blanket exception handling**

`ProviderConnectivityService.check()` must delegate to the probe service and return `capability`, `ok`, `error_code`, `message`, `suggested_action`, `provider_id`, `model_id`, `latency_ms`. Preserve the old `name`, `ok`, `message` keys in serialization for one compatibility cycle.

- [ ] **Step 6: Run discovery, probe and compatibility tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_discovery.py tests/providers/test_probes.py tests/providers/test_connectivity.py -q`

Expected: all tests pass; 401, balance, model permission, network, 429 and 5xx have different codes and copy.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 5 — discovery and capability probes — discovery/probe/connectivity tests — PASS` when Git is absent.

---

### Task 6: Route capabilities, apply bounded fallback, and hot-swap runtime snapshots

**Files:**
- Create: `backend/src/kerui_recruit/providers/router.py`
- Create: `backend/src/kerui_recruit/providers/manager.py`
- Create: `backend/tests/providers/test_router.py`
- Create: `backend/tests/providers/test_manager.py`
- Modify: `backend/src/kerui_recruit/providers/factory.py:29-132`
- Modify: `backend/src/kerui_recruit/runtime.py:1-494`
- Modify: `backend/src/kerui_recruit/api/services.py:39-69`
- Modify: `backend/tests/providers/test_factory.py`
- Modify: `backend/tests/test_runtime.py`

**Interfaces:**
- Consumes: v2 config, catalog, adapters and probes.
- Produces: `ProviderSnapshot`, `CapabilityRouter.generate()`, `AiProviderManager.update_routes()`, `AiProviderManager.activate_embedding_route()`, stable provider proxies used by existing pipelines.

- [ ] **Step 1: Write failing routing policy tests**

```python
@pytest.mark.asyncio
async def test_rate_limit_uses_one_authorized_fallback() -> None:
    router = router_with(primary=raises("E_API_RATE_LIMIT"), fallback=returns("ok"))
    result = await router.generate(TEXT_REQUEST)
    assert result.text == "ok"
    assert result.fallback_count == 1


@pytest.mark.asyncio
async def test_invalid_key_does_not_cross_provider() -> None:
    fallback = AsyncMock()
    router = router_with(primary=raises("E_API_KEY_INVALID"), fallback=fallback)
    with pytest.raises(ProviderError):
        await router.generate(TEXT_REQUEST)
    fallback.assert_not_awaited()


def test_embedding_change_stays_pending_until_rebuild_succeeds() -> None:
    manager = make_manager(active_embedding=OLD_ROUTE)
    manager.update_routes(routes={Capability.EMBEDDING: NEW_ROUTE})
    assert manager.snapshot.embedding_route == OLD_ROUTE
    assert manager.config.pending_embedding_route == NEW_ROUTE
```

- [ ] **Step 2: Run router/manager tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_router.py tests/providers/test_manager.py -q`

Expected: modules are missing.

- [ ] **Step 3: Implement explicit retry classification and immutable snapshots**

```python
FALLBACK_CODES = frozenset({
    "E_API_NETWORK", "E_API_TIMEOUT", "E_API_RATE_LIMIT",
    "E_API_SERVER", "E_API_MODEL_NOT_FOUND", "E_API_MODEL_FORBIDDEN",
})


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    revision: int
    generation: Mapping[Capability, tuple[GenerationClient, ...]]
    embedding: EmbeddingProvider
    reranker: RerankerProvider
    ocr: OCRProvider | None
```

Try primary then at most one fallback. Do not retry validation, policy/content, authentication or malformed-request errors. Include fallback metadata in `GenerationResult`, not raw provider payloads.

- [ ] **Step 4: Implement atomic manager swap and delayed close**

Protect swaps with `asyncio.Lock`. Build and probe the candidate snapshot before assignment. After assignment, allow in-flight calls holding the old snapshot to finish, then close its `httpx.AsyncClient`; manager shutdown must close every remaining client exactly once.

- [ ] **Step 5: Protect Embedding activation with existing index rebuild**

`update_routes()` writes an Embedding change to `pending_embedding_route`. `activate_embedding_route()` invokes the existing index rebuild service, keeps the active snapshot unchanged on failure, and swaps only after a successful rebuild. Treat same-dimension model changes as requiring rebuild.

- [ ] **Step 6: Inject stable proxies into current business services**

Refactor `build_providers()` to return proxies backed by `AiProviderManager.current_snapshot()` while preserving `ProviderBundle` fields. Update runtime construction for Resume/JD parsing, OCR, search rewrite, profile generation, org parsing, resume gate and BD Agent to use the same routed capability rather than separately resolving `text_*` values.

- [ ] **Step 7: Run provider and runtime regression**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers tests/test_runtime.py -q`

Expected: all provider and runtime tests pass; a runtime test changes a text route and observes the next request use the new snapshot without recreating the FastAPI app.

- [ ] **Step 8: Record the task checkpoint**

Append: `Task 6 — router, fallback and hot reload — provider/runtime tests — PASS` when Git is absent.

---

### Task 7: Expose the AI configuration API without leaking secrets

**Files:**
- Create: `backend/src/kerui_recruit/api/ai_settings.py`
- Create: `backend/tests/api/test_ai_settings.py`
- Modify: `backend/src/kerui_recruit/main.py:11-180`
- Modify: `backend/src/kerui_recruit/api/services.py:39-69`
- Modify: `backend/src/kerui_recruit/api/settings.py:12-71`
- Modify: `backend/src/kerui_recruit/providers/vendors.py`

**Interfaces:**
- Consumes: `CatalogService`, `AiConfigStore`, `ModelDiscoveryService`, `CapabilityProbeService`, `AiProviderManager`.
- Produces: all `/api/ai/*` endpoints defined in the spec and compatibility behavior for `/api/settings/vendors`.

- [ ] **Step 1: Write failing API contract and secret tests**

```python
def test_create_and_list_connection_never_returns_secret(client: TestClient) -> None:
    response = client.post("/api/ai/connections", json={
        "provider_id": "deepseek", "display_name": "我的 DeepSeek", "api_key": "sk-secret"
    })
    assert response.status_code == 201
    assert "sk-secret" not in response.text
    listed = client.get("/api/ai/connections")
    assert listed.json()[0]["has_secret"] is True
    assert "encrypted_api_key" not in listed.text


def test_delete_referenced_connection_reports_capabilities(client: TestClient) -> None:
    response = client.delete(f"/api/ai/connections/{TEXT_CONNECTION_ID}")
    assert response.status_code == 409
    assert response.json()["detail"]["capabilities"] == ["text_generation"]
```

Cover catalog refresh, discover, probe, route save, pending Embedding route, activation and unknown provider/model errors.

- [ ] **Step 2: Run API tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/api/test_ai_settings.py -q`

Expected: endpoints return 404.

- [ ] **Step 3: Implement request/response models and router**

Use Pydantic request models with `extra="forbid"`. Return `201` for connection creation, `204` for deletion, `409` for referenced deletion or pending Embedding rebuild, `422` for invalid catalog references, and `502` only for completed upstream probe failures. Use existing `ApiError` envelope and stable codes.

- [ ] **Step 4: Register services and router**

Add `ai_provider_manager` and catalog/config/discovery/probe services to `AppServices`. Include `ai_settings_router` in `main.create_app()` next to `settings_router`. All endpoints inherit the existing local session-token middleware.

- [ ] **Step 5: Preserve one-cycle compatibility**

`GET /api/settings/vendors` must map the active catalog to the old keys plus `deprecated: true`. `PUT /api/settings` must ignore a masked unchanged legacy secret but reject a newly supplied legacy AI field with `E_AI_SETTINGS_MOVED`; mail, reminders, Tavily and SerpApi fields continue to work.

- [ ] **Step 6: Run API, settings and auth tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/api/test_ai_settings.py tests/providers/test_ai_config_store.py tests/test_runtime.py -q`

Expected: all tests pass and response bodies never contain test secrets.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 7 — AI configuration API — API/config/runtime tests — PASS` when Git is absent.

---

### Task 8: Add typed frontend API state before changing the page

**Files:**
- Create: `desktop/src/ai/types.ts`
- Modify: `desktop/src/api/client.ts:1-1060`
- Modify: `desktop/src/App.tsx:683-879,1244-1246,2477-2535`
- Modify: `desktop/tests/api-client.test.ts`
- Modify: `desktop/tests/App.test.tsx:600-640,904-918`

**Interfaces:**
- Consumes: Task 7 JSON contracts.
- Produces: `ProviderSummary`, `AiConnection`, `DiscoveredModel`, `ProbeResult`, `AiRoutes`, `AiCatalogStatus` and corresponding `BackendApi` methods/state handlers.

- [ ] **Step 1: Write failing client request tests**

```typescript
it("creates a connection and probes it without putting the key in a URL", async () => {
  const client = makeClient();
  await client.createAiConnection({ provider_id: "deepseek", display_name: "DeepSeek", api_key: "sk-secret" });
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/ai/connections"),
    expect.objectContaining({ method: "POST", body: expect.stringContaining("sk-secret") }));
  expect(fetchMock.mock.calls[0][0]).not.toContain("sk-secret");
});
```

Add cases for list catalog/connections/routes, refresh catalog, discover, probe, update routes and activate Embedding.

- [ ] **Step 2: Run frontend tests and confirm red state**

Run: `Set-Location desktop; npm test -- --run tests/api-client.test.ts`

Expected: TypeScript fails because new methods and types are absent.

- [ ] **Step 3: Define the API types in `desktop/src/ai/types.ts`**

```typescript
export type Capability = "text_generation" | "vision_generation" | "embedding" | "rerank";
export interface AiConnection {
  connection_id: string;
  provider_id: string;
  display_name: string;
  enabled: boolean;
  has_secret: boolean;
  masked_secret: string;
  base_url_override: string | null;
}
export interface ProbeResult {
  capability: Capability;
  ok: boolean;
  error_code: string | null;
  message: string;
  suggested_action: string | null;
  model_id: string;
  latency_ms: number;
}
```

Move only AI-specific types out of `App.tsx`; keep unrelated application types where they are to avoid an unrelated refactor.

- [ ] **Step 4: Implement the typed client methods**

Use the existing authenticated `request<T>()` helper. URL-encode connection IDs and never serialize `undefined`. Keep API Key only in the create/update request object and clear the caller's form state after resolution.

- [ ] **Step 5: Replace old App AI settings state**

Store `catalog`, `connections`, `routes`, `probeResults` and wizard-open state. Keep mail/backup settings in the existing `AppSettings`. Remove the “重启应用后生效” success message for AI routes; show “已生效” or “等待重建索引”.

- [ ] **Step 6: Run typecheck and focused tests**

Run: `Set-Location desktop; npm test -- --run tests/api-client.test.ts tests/App.test.tsx`

Expected: tests pass and old fake API fixtures implement the new methods.

- [ ] **Step 7: Record the task checkpoint**

Append: `Task 8 — typed frontend API state — api-client/App tests — PASS` when Git is absent.

---

### Task 9: Replace the complex AI form with a guided panel and advanced routing

**Files:**
- Create: `desktop/src/ai/AiServicesPanel.tsx`
- Create: `desktop/src/ai/ConnectionWizard.tsx`
- Create: `desktop/src/ai/AdvancedRoutingPanel.tsx`
- Create: `desktop/tests/AiServicesPanel.test.tsx`
- Create: `desktop/tests/ConnectionWizard.test.tsx`
- Create: `desktop/tests/AdvancedRoutingPanel.test.tsx`
- Modify: `desktop/src/pages/SettingsPage.tsx:1-430`
- Modify: `desktop/src/styles.css`

**Interfaces:**
- Consumes: Task 8 types and callbacks; existing `Button`, `Modal`, `FormControl`, `StatusBadge` components.
- Produces: accessible AI settings workflow embedded in `SettingsPage`.

- [ ] **Step 1: Write failing behavior tests for the default simple view**

```typescript
it("shows status and one primary action without technical fields", async () => {
  render(<AiServicesPanel {...readyProps} />);
  expect(screen.getByRole("button", { name: "添加 AI 服务" })).toBeVisible();
  expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
  expect(screen.queryByText("temperature")).not.toBeInTheDocument();
});

it("completes the wizard with provider and key only", async () => {
  const user = userEvent.setup();
  render(<ConnectionWizard {...wizardProps} />);
  await user.click(screen.getByRole("button", { name: "DeepSeek" }));
  await user.type(screen.getByLabelText("API Key"), "sk-secret");
  await user.click(screen.getByRole("button", { name: "检测并继续" }));
  expect(await screen.findByText("系统推荐方案")).toBeVisible();
  expect(screen.getByLabelText("API Key")).toHaveValue("");
});
```

Add tests for failed probe action copy, manual catalog refresh, keyboard focus, advanced-panel disclosure, deleting a referenced connection, and Embedding rebuild confirmation.

- [ ] **Step 2: Run component tests and confirm red state**

Run: `Set-Location desktop; npm test -- --run tests/AiServicesPanel.test.tsx tests/ConnectionWizard.test.tsx tests/AdvancedRoutingPanel.test.tsx`

Expected: component imports fail.

- [ ] **Step 3: Implement `AiServicesPanel`**

Render one overall status (`未配置`、`部分可用`、`全部可用`), current catalog version/staleness, connection cards and a single primary button. Each card shows provider, enabled capabilities and latest probe result; do not show full Base URL or model IDs until the advanced disclosure opens.

- [ ] **Step 4: Implement the four-step `ConnectionWizard`**

Step order is provider → Key → discover/probe → recommended route. Default provider list comes entirely from the backend catalog. Put Base URL override behind “自定义地址”; label it “仅在供应商文档要求时填写”. Disable confirmation until at least one generation or search capability probe succeeds.

- [ ] **Step 5: Implement `AdvancedRoutingPanel`**

Provide one row per capability with primary and optional backup selectors filtered to compatible, enabled, successfully probed models. Embedding change opens a confirmation explaining that the old model remains active during rebuild; call `activateEmbeddingRoute()` only after explicit confirmation.

- [ ] **Step 6: Recompose `SettingsPage` and add responsive CSS**

Keep existing mail, recycle bin, backup and migration features, but group them under separate headings after AI services. At widths below 720px, stack cards and wizard actions vertically. Preserve existing visible labels used by mail/backup tests.

- [ ] **Step 7: Run component and App regression**

Run: `Set-Location desktop; npm test -- --run tests/AiServicesPanel.test.tsx tests/ConnectionWizard.test.tsx tests/AdvancedRoutingPanel.test.tsx tests/App.test.tsx`

Expected: all tests pass; the old test expecting an AI restart message is replaced by an immediate-active assertion.

- [ ] **Step 8: Record the task checkpoint**

Append: `Task 9 — guided AI settings UI — AI component/App tests — PASS` when Git is absent.

---

### Task 10: Package, exercise end to end, and establish release gates

**Files:**
- Create: `desktop/tests/ai-settings.spec.ts`
- Create: `docs/verification/ai-provider-acceptance.md`
- Modify: `使用说明.md`
- Modify: `kerui-recruit-sidecar.spec:1-18`
- Modify: `backend/tests/test_sidecar.py`

**Interfaces:**
- Consumes: completed backend API, manager, packaged catalog and frontend workflow.
- Produces: repeatable acceptance evidence and a production-build checklist.

- [ ] **Step 1: Add an E2E test for the nontechnical happy path**

```typescript
test("configures an AI service with provider and key only", async ({ page }) => {
  await page.getByText("设置").click();
  await page.getByRole("button", { name: "添加 AI 服务" }).click();
  await page.getByRole("button", { name: "DeepSeek" }).click();
  await page.getByLabel("API Key").fill("e2e-test-key");
  await page.getByRole("button", { name: "检测并继续" }).click();
  await expect(page.getByText("系统推荐方案")).toBeVisible();
  await page.getByRole("button", { name: "启用推荐方案" }).click();
  await expect(page.getByText("已生效")).toBeVisible();
});
```

Mock upstream AI at the backend transport boundary so no real Key, quota or Internet access is required. Add an E2E case where a 429 primary falls back to the configured backup and the UI displays a nonblocking fallback diagnostic.

- [ ] **Step 2: Run the complete automated suite**

Run: `Set-Location backend; py -3.12 -m pytest -q`

Expected: complete backend suite passes.

Run: `Set-Location desktop; npm test -- --run`

Expected: complete Vitest suite passes.

Run: `Set-Location desktop; npx playwright test tests/ai-settings.spec.ts`

Expected: AI settings E2E passes.

- [ ] **Step 3: Build and smoke-test the sidecar in offline mode**

Run: `py -3.12 -m PyInstaller --noconfirm kerui-recruit-sidecar.spec`

Expected: build succeeds and `dist/kerui-recruit-sidecar.exe` exists.

Start the packaged sidecar with a temporary data root, no network and no AI environment variables. Call `GET /api/ai/catalog`; expected result contains every built-in provider and reports source `builtin`.

- [ ] **Step 4: Verify the production catalog build gate**

Run with `KERUI_RELEASE_BUILD=1` and both catalog environment variables unset.

Expected: PyInstaller stops with a message naming `KERUI_PROVIDER_CATALOG_URL` and `KERUI_PROVIDER_CATALOG_PUBLIC_KEY_B64`.

Run again with a test HTTPS URL and test public key.

Expected: build succeeds, the executable contains only the public key, and `rg` over `dist`/build logs finds no signing private key material.

- [ ] **Step 5: Write the operator and user acceptance documents**

`使用说明.md` must explain the four-step workflow, how to obtain a Key through each provider's official console link supplied by the live catalog, what the four capability names mean in plain Chinese, and why Embedding changes rebuild indexes. `docs/verification/ai-provider-acceptance.md` must record exact suite commands, pass counts, packaged executable hash, active catalog revision, offline result and production-gate result.

- [ ] **Step 6: Perform the final secret and legacy scan**

Run: `rg -n "api_key.*print|logger.*api_key|reasoning_content.*(log|print)|sk-[A-Za-z0-9]{12,}" backend/src desktop/src`

Expected: no logging/printing of API Keys or reasoning content; any catalog model ID that begins with `sk-` must be reviewed as a false positive.

Run: `rg -n "deepseek_api_key|siliconflow_api_key|text_api_key|vision_api_key|embedding_api_key|rerank_api_key" desktop/src`

Expected: no matches in the new frontend.

- [ ] **Step 7: Record the release checkpoint**

Append the final test counts, executable path, SHA-256 hash and catalog revision to `.trae/ai-provider-implementation-progress.md`. If the directory has become a Git repository by this point, commit only the files from this plan with message `feat: add resilient AI provider configuration`.

---

## Delivery checkpoints

- After Task 3: old installations can migrate safely, but the old UI remains in use.
- After Task 6: backend routing, reasoning-model handling, fallback and hot reload are complete behind existing callers.
- After Task 9: the nontechnical configuration experience is complete.
- After Task 10: the feature is releasable only if the signed catalog production gate and offline packaged smoke test pass.

## Rollback strategy

- Before Task 7, the new subsystem is not exposed and can be disabled by continuing to construct the legacy `ProviderBundle`.
- After Task 7, keep v2 files additive and preserve legacy settings. A rollback build ignores `ai-providers.json` and reads the old fields.
- Never delete `provider-catalog-cache.json` automatically; if a new catalog is rejected, the loader selects the last valid revision.
- An Embedding activation failure restores the prior route and prior index pointer; it does not mutate the active route until rebuild success.

## Definition of done

- All ten spec acceptance criteria have a passing automated or packaged acceptance check.
- A fresh user completes configuration using only provider selection and API Key.
- A migrated user retains working routes without re-entering secrets.
- Changing a text model takes effect without restarting; changing Embedding cannot take effect without rebuild.
- Catalog tampering, network loss, model removal, 429 and provider 5xx all degrade according to the specified bounded policy.
- No production package can be emitted with remote updates claimed as enabled while its catalog URL or Ed25519 public key is absent.
