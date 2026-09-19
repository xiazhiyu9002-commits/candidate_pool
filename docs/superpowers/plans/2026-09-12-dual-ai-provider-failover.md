# Domestic AI Dual-Provider Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-09-13 current-code revision:** This plan was re-audited after the search, profile and parse-quality changes landed. When an older example conflicts with a revision note or with the implementation-time source, preserve the current source behavior and follow the revision.

**Goal:** 让个人猎头默认使用 DeepSeek，只需填写一个 API Key 即可工作，并可再配置一个不同供应商作为备用；主服务不可用或拥堵时，在一次业务请求内安全切换到备用服务。

**Architecture:** 生成式 AI 被收敛到稳定的 `GenerationClient` 协议、声明式供应商目录、OpenAI Chat Completions 适配器和有界主备路由器。业务模块只声明快速文本、思考文本或视觉角色；`AiProviderManager` 持有可热切换快照，配置最多两个用户授权连接，主服务失败后最多调用备用一次。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、httpx、cryptography/AES-GCM、pytest；React 19、TypeScript 5.9、Vite 7、Vitest、Testing Library、Playwright；PyInstaller、Tauri 2。

**Spec:** `docs/superpowers/specs/2026-09-12-dual-ai-provider-failover-design.md`

## Global Constraints

- 本计划取代 `docs/superpowers/plans/2026-09-11-resilient-ai-provider-architecture.md` 中“生成式 AI 供应商改造”部分；两份计划不得并行执行。
- 当前检索改造已经落地并有 `.trae/search-implementation-progress.md` 记录；Trae 开始前仍须重新查看 `runtime.py`、`providers/factory.py`、`search/service.py`、`search/rewrite.py`、`resumes/profile.py`、`jd/profile.py`、`backfill/service.py`、`resumes/validity.py`、`App.tsx`、`api/client.ts`、`SettingsPage.tsx` 和相关测试的最新内容，不得覆盖已有改动。
- 本次不修改 Embedding、Rerank、Tavily、SerpApi、当前 `INDEX_SCHEMA_VERSION="8"` / `INDEX_CHUNK_VERSION="5"`、LanceDB schema、检索阈值、父子 chunk、查询计划、方向字段或十种检索/匹配模式。
- Python 保持 `>=3.12,<3.13`，不引入 LiteLLM 等统一网关依赖。
- 首版只实现 OpenAI Chat Completions 兼容协议，不实现 Responses API 或 Anthropic Messages。
- 新用户默认预选 DeepSeek；任何供应商都不能在没有用户 API Key 的情况下接收业务数据。
- 最多启用两个生成式 AI 连接。一个连接可用，两个连接为推荐；主备应优先来自不同供应商。
- 每个业务请求最多两个远程生成调用：主服务一次、备用服务一次。禁止无限重试或备用失败后回到主服务。
- Kimi 开放平台与 Kimi Code 使用独立 provider ID、Base URL、Key 和错误提示；Kimi Code 固定为 `interactive_only`。
- `reasoning_content` 不记录、不返回、不保存，也不能替代最终 `content`。
- API Key 使用现有 `EncryptionService` 加密；GET、日志、异常和测试快照不得出现明文或密文。
- 配置探测只能使用固定虚构文本和内置测试图片，不能上传真实简历或 JD。
- 保存生成式 AI 配置后立即热生效；不得要求用户重启应用。
- 旧生成式 AI 设置迁移幂等，旧 Embedding/Rerank/搜索设置保持原样。
- 搜索的绝对 deadline、子预算和取消传播必须保持。`asyncio.CancelledError` 不能变成可切换错误；共享 deadline 到期后不得开始备用调用。
- `check_parsed_resume` 的 `E_PARSE_INCOMPLETE` 是业务质量门槛，不是通用供应商故障；不得通过路由器自动二次调用来绕过现有“不合格 → 视觉重解析”流程。
- 候选人/JD 画像的 `generate(data, instruction=None, previous=None)` 增量语义与简历/JD 的 `direction` 字段必须保留。
- 当前目录没有 `.git`。每个任务结束时执行 `Test-Path .git`；若仍为 `False`，不要初始化仓库，把任务编号、变更文件和测试结果追加到 `.trae/dual-ai-provider-progress.md`。若已经存在 Git，则按任务提交，且不得提交无关改动。

### Preflight baseline (must be refreshed before Task 1)

- [ ] Run `Set-Location backend; py -3.12 -m pytest tests/providers/test_deepseek.py tests/providers/test_factory.py tests/providers/test_connectivity.py tests/search/test_rewrite.py tests/backfill/test_backfill.py tests/resumes/test_validity.py tests/test_runtime.py -q` and record the exact result before editing.
- [ ] The 2026-09-13 audit result was `1 failed, 44 passed`: `tests/search/test_rewrite.py::test_rewrite_cache_misses_on_lexicon_version_change` sets `LEXICON_VERSION` to `"2"`, while production is already `"2"`. Replace this stale assumption when Task 6 switches the cache key to `cache_identity`; do not weaken cache invalidation coverage.
- [ ] Re-run the full backend suite once before implementation. The latest recorded full baseline in `.trae/search-implementation-progress.md` was `4 failed, 666 passed, 2 skipped, 1 deselected`, with four named timing-sensitive search tests. Do not assume the count is still four: record the fresh exact names and compare again at final acceptance.
- [ ] New and AI-affected focused tests must be fully green. Full-suite acceptance means “no new failures compared with the recorded preflight”; inherited failures must be reproduced and named, never hidden or described as all-green.

---

## File Map

### New backend files

- `backend/src/kerui_recruit/providers/ai/__init__.py`：导出稳定公共接口。
- `backend/src/kerui_recruit/providers/ai/contracts.py`：角色、任务场景、统一请求/响应和客户端协议。
- `backend/src/kerui_recruit/providers/ai/catalog_models.py`：供应商目录和模型档案 Pydantic 类型。
- `backend/src/kerui_recruit/providers/ai/provider_catalog.builtin.json`：七个接入入口的内置数据。
- `backend/src/kerui_recruit/providers/ai/catalog.py`：内置、缓存、可选远程签名目录加载与模型发现结果合并。
- `backend/src/kerui_recruit/providers/ai/config_models.py`：最多两个连接的配置模型。
- `backend/src/kerui_recruit/providers/ai/config_store.py`：加密、原子保存和旧设置迁移。
- `backend/src/kerui_recruit/providers/ai/parameter_mapping.py`：思考、JSON 和可选参数映射。
- `backend/src/kerui_recruit/providers/ai/openai_chat.py`：OpenAI Chat Completions 请求与响应适配。
- `backend/src/kerui_recruit/providers/ai/probes.py`：模型发现和无隐私能力探测。
- `backend/src/kerui_recruit/providers/ai/circuit_breaker.py`：连接/模型健康与冷却状态。
- `backend/src/kerui_recruit/providers/ai/router.py`：主备选择、错误分类和单次故障切换。
- `backend/src/kerui_recruit/providers/ai/task_client.py`：为旧业务类提供 `complete_json`/`complete_text` 外观。
- `backend/src/kerui_recruit/providers/ai/manager.py`：配置热加载、不可变快照、状态与生命周期。
- `backend/src/kerui_recruit/providers/generation_tasks.py`：供应商无关的简历/JD解析类及原提示词。
- `backend/src/kerui_recruit/api/ai_settings.py`：`/api/ai/*` 接口。
- `scripts/sign_ai_provider_catalog.py`：发布方离线生成 Ed25519 签名目录信封；私钥不进入安装包。

### New backend tests

- `backend/tests/providers/ai/test_catalog.py`
- `backend/tests/providers/ai/test_config_store.py`
- `backend/tests/providers/ai/test_parameter_mapping.py`
- `backend/tests/providers/ai/test_openai_chat.py`
- `backend/tests/providers/ai/test_probes.py`
- `backend/tests/providers/ai/test_router.py`
- `backend/tests/providers/ai/test_manager.py`
- `backend/tests/api/test_ai_settings.py`

### Modified backend files

- `backend/src/kerui_recruit/providers/errors.py`
- `backend/src/kerui_recruit/providers/openai_compatible.py`
- `backend/src/kerui_recruit/providers/deepseek.py`
- `backend/src/kerui_recruit/providers/factory.py`
- `backend/src/kerui_recruit/providers/connectivity.py`
- `backend/src/kerui_recruit/providers/vendors.py`
- `backend/src/kerui_recruit/providers/ocr.py`
- `backend/src/kerui_recruit/providers/vision_parse.py`
- `backend/src/kerui_recruit/providers/leads.py`
- `backend/src/kerui_recruit/mail/resume_gate.py`
- `backend/src/kerui_recruit/org/import_parser.py`
- `backend/src/kerui_recruit/bd_agent/planner.py`
- `backend/src/kerui_recruit/bd_agent/synthesis.py`
- `backend/src/kerui_recruit/search/rewrite.py`
- `backend/src/kerui_recruit/search/service.py`
- `backend/src/kerui_recruit/resumes/profile.py`
- `backend/src/kerui_recruit/jd/profile.py`
- `backend/src/kerui_recruit/backfill/service.py`
- `backend/src/kerui_recruit/resumes/pipeline.py`
- `backend/src/kerui_recruit/core/settings.py`
- `backend/src/kerui_recruit/core/settings_service.py`
- `backend/src/kerui_recruit/core/settings_store.py`
- `backend/src/kerui_recruit/sidecar.py`
- `backend/src/kerui_recruit/runtime.py`
- `backend/src/kerui_recruit/api/services.py`
- `backend/src/kerui_recruit/api/settings.py`
- `backend/src/kerui_recruit/main.py`
- 以上文件现有对应测试。

### New frontend files

- `desktop/src/ai/types.ts`：目录、连接、探测、状态和更新命令类型。
- `desktop/src/ai/AiServicesPanel.tsx`：AI 配置总览、主备卡片和状态。
- `desktop/src/ai/ConnectionWizard.tsx`：选择供应商、Key、探测、确认四步向导。
- `desktop/src/ai/AdvancedAiSettings.tsx`：模型、Base URL、兼容风格和主备顺序。
- `desktop/tests/AiServicesPanel.test.tsx`
- `desktop/tests/ConnectionWizard.test.tsx`
- `desktop/tests/AdvancedAiSettings.test.tsx`
- `desktop/tests/ai-settings.spec.ts`

### Modified frontend and package files

- `desktop/src/api/client.ts`
- `desktop/src/App.tsx`
- `desktop/src/pages/SettingsPage.tsx`
- `desktop/src/styles.css`
- `desktop/tests/api-client.test.ts`
- `desktop/tests/App.test.tsx`
- `kerui-recruit-sidecar.spec`
- `backend/tests/test_sidecar.py`
- `使用说明.md`

---

### Task 1: Establish stable generation contracts and the seven-entry provider catalog

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/__init__.py`
- Create: `backend/src/kerui_recruit/providers/ai/contracts.py`
- Create: `backend/src/kerui_recruit/providers/ai/catalog_models.py`
- Create: `backend/src/kerui_recruit/providers/ai/provider_catalog.builtin.json`
- Create: `backend/src/kerui_recruit/providers/ai/catalog.py`
- Create: `scripts/sign_ai_provider_catalog.py`
- Create: `backend/tests/providers/ai/test_catalog.py`
- Modify: `backend/src/kerui_recruit/providers/vendors.py`

**Interfaces:**
- Produces: `ModelRole`, `ExecutionContext`, `ReasoningMode`, `OutputMode`, `TaskKind`, `GenerationRequest`, `GenerationResult`, `AttemptDiagnostic`, `GenerationClient`, `ProviderPreset`, `ModelProfile`, `ProviderCatalog`, `CatalogService.load()`.
- Consumes: bundled JSON file and optional cached signed catalog; no user credentials.

- [ ] **Step 1: Write failing contract and catalog tests**

```python
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole


def test_catalog_has_exact_supported_entries(tmp_path):
    catalog = CatalogService(cache_path=tmp_path / "catalog.json").load()
    assert set(catalog.providers) == {
        "deepseek", "kimi_open", "kimi_code", "qwen",
        "zhipu", "siliconflow", "custom_openai",
    }
    assert catalog.default_provider_id == "deepseek"


def test_kimi_platforms_cannot_be_conflated(tmp_path):
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert providers["kimi_open"].base_url == "https://api.moonshot.cn/v1"
    assert providers["kimi_code"].base_url == "https://api.kimi.com/coding/v1"
    assert providers["kimi_open"].allowed_contexts == set(ExecutionContext)
    assert providers["kimi_code"].allowed_contexts == {ExecutionContext.INTERACTIVE}


def test_every_recommended_model_declares_its_role(tmp_path):
    catalog = CatalogService(cache_path=tmp_path / "catalog.json").load()
    for provider in catalog.providers.values():
        for role, model_id in provider.recommended_models.items():
            assert role in provider.models[model_id].roles
            assert role in set(ModelRole)
```

- [ ] **Step 2: Run the tests and confirm the modules are absent**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_catalog.py -q`

Expected: collection fails because the `providers.ai` package does not exist.

- [ ] **Step 3: Define the stable request/response contracts**

Implement these names and values exactly in `contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ModelRole(StrEnum):
    FAST_TEXT = "fast_text"
    REASONING_TEXT = "reasoning_text"
    VISION = "vision"


class ExecutionContext(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    BATCH = "batch"


class ReasoningMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


class OutputMode(StrEnum):
    TEXT = "text"
    JSON = "json"


class TaskKind(StrEnum):
    RESUME_PARSE = "resume_parse"
    JD_PARSE = "jd_parse"
    QUERY_REWRITE = "query_rewrite"
    CANDIDATE_PROFILE = "candidate_profile"
    JD_PROFILE = "jd_profile"
    ORG_PARSE = "org_parse"
    LEAD_EXTRACT = "lead_extract"
    MAIL_RESUME_GATE = "mail_resume_gate"
    BD_PLAN = "bd_plan"
    BD_SYNTHESIS = "bd_synthesis"
    OCR = "ocr"
    VISION_PARSE = "vision_parse"


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    messages: list[dict[str, Any]]
    role: ModelRole
    task_kind: TaskKind
    execution_context: ExecutionContext
    output_mode: OutputMode
    reasoning: ReasoningMode
    reasoning_effort: Literal["low", "high", "max"] | None = None
    response_model: type[BaseModel] | None = None
    temperature: float | None = None
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class AttemptDiagnostic:
    connection_id: str
    provider_id: str
    model: str
    error_code: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    parsed: BaseModel | None
    connection_id: str
    provider_id: str
    model: str
    fallback_used: bool = False
    attempts: tuple[AttemptDiagnostic, ...] = field(default_factory=tuple)


class GenerationClient(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
```

- [ ] **Step 4: Define catalog schemas with executable-data restrictions**

`catalog_models.py` must use `extra="forbid"`. `ProviderPreset` includes `provider_id`, `label`, `base_url`, `parameter_style`, `allowed_contexts`, `models`, `recommended_models`, `help_url`, `key_help_url`, and `subscription_warning`. `ModelProfile` includes `model_id`, `roles`, `supported_reasoning_modes`, `supported_reasoning_efforts`, `supports_json_schema`, `supports_json_object`, `supports_temperature`, and `deprecated`. Use explicit sets because current models include both switchable-thinking models and always-thinking models; a single `supports_reasoning_toggle` flag is not enough to safely satisfy `reasoning=off`.

Reject catalog entries when `base_url` is not HTTPS, `parameter_style` is outside the seven known styles, a recommended model is missing, or a model claims no role.

- [ ] **Step 5: Add the built-in catalog with current conservative defaults**

Use these provider IDs, official endpoints and initial role recommendations:

| provider_id | Base URL | fast_text | reasoning_text | vision |
| --- | --- | --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `deepseek-v4-flash` | `deepseek-v4-pro` | `deepseek-v4-flash-vision-exp` |
| `kimi_open` | `https://api.moonshot.cn/v1` | `kimi-k2.6` | `kimi-k3` | `kimi-k2.6` |
| `kimi_code` | `https://api.kimi.com/coding/v1` | `kimi-for-coding` | `kimi-for-coding` | probe only; interactive context only |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` | `qwen3.8-max` | `qwen3.8-flash` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.7-flashx` | `glm-5.3` | `glm-5.3-flash` |
| `siliconflow` | `https://api.siliconflow.cn/v1` | discovered model preferred; seed `deepseek-ai/DeepSeek-V4-Flash` | discovered model preferred; seed `deepseek-ai/DeepSeek-V4-Pro` | discovered model preferred; seed a currently available GLM vision model |
| `custom_openai` | user input | user input | user input | user input |

Kimi K3 is always-thinking, so it must not be put in the `fast_text + reasoning=off` route. K2.6 remains the conservative fast/vision seed while it is available. Kimi Code remains an advanced, warned, `interactive_only` entry and is not a recommended backup for product workloads. Before committing the JSON, compare every model ID with the official links in the spec and call `/models` where supported. If an official page changed, update only catalog data/capabilities and record the observed replacement; do not add another provider. If current docs and an API enumeration disagree, prefer a successful real probe and keep the older verified model as a fallback catalog entry rather than guessing.

- [ ] **Step 6: Implement catalog loading priority**

`CatalogService.load()` chooses a valid cached catalog when its version is newer than the built-in version; otherwise it loads the bundled file. A corrupt, invalid or older cache is ignored. `refresh()` is optional when no remote URL/public key is configured and must return a typed `CatalogRefreshResult(status="disabled", active_version=...)`, not raise.

Remote catalog data may update models, capabilities, deprecation flags and help links. Reject attempts to introduce a new `parameter_style`, arbitrary headers, executable expressions or more than the seven provider IDs.

Use environment variables `KERUI_AI_CATALOG_URL` and `KERUI_AI_CATALOG_PUBLIC_KEY_B64`. The HTTPS response is an envelope with exactly `catalog` and `signature` fields. Verify the signature over UTF-8 canonical JSON produced by `json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` using `Ed25519PublicKey`. Reject non-HTTPS URLs, responses over 1 MiB, invalid base64, invalid signatures, schema failures and version rollback. Use a 5-second connect/read timeout and atomically replace the cache only after every check passes.

`scripts/sign_ai_provider_catalog.py` accepts `--catalog`, `--private-key-b64` and `--output`, emits that exact envelope, and never writes or prints the private key. Unit tests generate a temporary Ed25519 keypair, accept a correctly signed newer catalog, and reject a modified payload.

- [ ] **Step 7: Keep the old vendor endpoint as a compatibility projection**

Change `providers/vendors.py` to generate the legacy `VendorPreset` list from the built-in catalog. Do not retain MiniMax or stale model IDs in the new AI UI. The old endpoint may continue returning a `deprecated: true` flag for one compatibility cycle.

- [ ] **Step 8: Run focused tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_catalog.py -q`

Expected: all catalog tests pass.

- [ ] **Step 9: Record or commit Task 1**

If Git exists:

```powershell
git add backend/src/kerui_recruit/providers/ai backend/src/kerui_recruit/providers/vendors.py backend/tests/providers/ai/test_catalog.py scripts/sign_ai_provider_catalog.py
git commit -m "feat: add generation provider catalog"
```

Otherwise append `Task 1 — generation contracts and catalog — PASS` plus the test command to `.trae/dual-ai-provider-progress.md`.

---

### Task 2: Add encrypted two-connection configuration and idempotent migration

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/config_models.py`
- Create: `backend/src/kerui_recruit/providers/ai/config_store.py`
- Create: `backend/tests/providers/ai/test_config_store.py`
- Modify: `backend/src/kerui_recruit/core/settings_store.py`
- Modify: `backend/src/kerui_recruit/core/settings.py`
- Modify: `backend/src/kerui_recruit/core/settings_service.py`
- Modify: `backend/src/kerui_recruit/sidecar.py`
- Test: `backend/tests/core/test_settings_service.py`
- Test: `backend/tests/test_sidecar.py`

**Interfaces:**
- Consumes: `ProviderCatalog`, existing `EncryptionService`, legacy `settings.json`.
- Produces: `AiConnection`, `AiProviderConfig`, `AiConfigStore.load()`, `save()`, `migrate_legacy()`.

- [ ] **Step 1: Write failing storage limits, encryption and migration tests**

```python
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
    write_legacy_settings(tmp_path, text_key="custom-key", deepseek_key="deepseek-key")
    config = make_store(tmp_path).migrate_legacy()
    assert [item.provider_id for item in config.connections] == ["custom_openai", "deepseek"]
    assert make_store(tmp_path).migrate_legacy() == config
```

- [ ] **Step 2: Run the tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_config_store.py -q`

Expected: collection fails because configuration models do not exist.

- [ ] **Step 3: Define configuration models**

Implement these stable fields:

```python
class AiConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str
    provider_id: str
    display_name: str
    api_key: SecretStr
    base_url_override: str | None = None
    models: dict[ModelRole, str] = Field(default_factory=dict)
    enabled: bool = True


class AiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    connections: list[AiConnection] = Field(default_factory=list, max_length=2)
    catalog_version: int = 1
```

Validate unique connection IDs. Validate provider IDs against the active catalog in `AiConfigStore`, not with a growing hard-coded enum. For `custom_openai`, require an HTTPS `base_url_override`; allow loopback HTTP only when `allow_insecure_loopback=True` was passed by a test/development caller.

- [ ] **Step 4: Implement encrypted disk serialization and public projection**

The disk JSON uses `encrypted_api_key`; the in-memory model uses `SecretStr`. `public_view()` returns connection ID, provider ID, display name, masked key, `has_api_key`, Base URL override only for custom connections, models and enabled state. It never returns `api_key` or `encrypted_api_key`.

Make `SettingsStore.save()` atomic for all settings files: write a sibling `.tmp`, flush and `os.fsync()`, copy the previous valid file to `.bak`, then `os.replace()`. A failed write must leave the previous file readable.

- [ ] **Step 5: Implement legacy migration without touching search settings**

Migration precedence must preserve existing behavior:

1. Old `text_api_key/text_base_url/text_model` becomes primary.
2. A distinct old DeepSeek Key becomes the next connection.
3. If fewer than two connections exist, old SiliconFlow text Key may fill the remaining slot.
4. Old vision fields merge into the matching connection by decrypted Key + normalized Base URL.
5. `embedding_*`, `rerank_*`, SiliconFlow embedding/rerank fields, Tavily, SerpApi and mail fields remain only in legacy settings.

The existence of a valid `ai-providers.json` stops future migrations. Do not delete any legacy fields.

- [ ] **Step 6: Keep runtime environment compatibility**

In `sidecar.py`, preserve all existing environment variables. `AiConfigStore` is authoritative when its file exists; otherwise migration/env construction feeds the initial in-memory config. Do not remove legacy `Settings` fields yet because search provider construction still depends on them.

- [ ] **Step 7: Run storage regression tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_config_store.py tests/core/test_settings_service.py tests/test_sidecar.py -q`

Expected: encryption, masking, atomic-write and sidecar tests pass; no search field is changed by migration.

- [ ] **Step 8: Record or commit Task 2**

If Git exists:

```powershell
git add backend/src/kerui_recruit/providers/ai/config_models.py backend/src/kerui_recruit/providers/ai/config_store.py backend/src/kerui_recruit/core/settings_store.py backend/src/kerui_recruit/core/settings.py backend/src/kerui_recruit/core/settings_service.py backend/src/kerui_recruit/sidecar.py backend/tests/providers/ai/test_config_store.py backend/tests/core/test_settings_service.py backend/tests/test_sidecar.py
git commit -m "feat: store two encrypted ai connections"
```

Otherwise append `Task 2 — encrypted two-connection config and migration — PASS` to the progress file.

---

### Task 3: Implement provider-specific request mapping and normalized Chat Completions responses

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/parameter_mapping.py`
- Create: `backend/src/kerui_recruit/providers/ai/openai_chat.py`
- Create: `backend/tests/providers/ai/test_parameter_mapping.py`
- Create: `backend/tests/providers/ai/test_openai_chat.py`
- Modify: `backend/src/kerui_recruit/providers/errors.py`
- Modify: `backend/src/kerui_recruit/providers/openai_compatible.py`
- Test: `backend/tests/providers/test_error_mapping.py`

**Interfaces:**
- Consumes: `GenerationRequest`, `AiConnection`, `ProviderPreset`, `ModelProfile`.
- Produces: `OpenAIChatAdapter.generate(request, model)`, `FailureCategory`, enhanced `ProviderError`.

- [ ] **Step 1: Write failing parameter mapping tests**

```python
@pytest.mark.parametrize(
    ("style", "mode", "expected"),
    [
        ("deepseek", ReasoningMode.REQUIRED, {"thinking": {"type": "enabled"}}),
        ("deepseek", ReasoningMode.OFF, {"thinking": {"type": "disabled"}}),
        ("kimi_open", ReasoningMode.REQUIRED, {"thinking": {"type": "enabled"}}),
        ("qwen", ReasoningMode.REQUIRED, {"enable_thinking": True}),
        ("zhipu", ReasoningMode.OFF, {"thinking": {"type": "disabled"}}),
        ("siliconflow", ReasoningMode.OFF, {"enable_thinking": False}),
        ("standard", ReasoningMode.REQUIRED, {}),
    ],
)
def test_reasoning_mapping(style, mode, expected):
    body = {}
    apply_reasoning(body, style=style, mode=mode, effort=None, profile=profile())
    for key, value in expected.items():
        assert body[key] == value
```

Also assert that `reasoning_effort` is sent only when the model profile supports it, `temperature` is omitted when unsupported, and JSON response format is omitted when unsupported.

Add capability-selection cases for current model behavior:

- Kimi K3 declares only `AUTO/REQUIRED`, never `OFF`; a fast non-thinking request must not select it.
- Kimi K2.6, DeepSeek V4 and Qwen 3.8 switchable models may declare both `OFF` and `REQUIRED` only after probe success.
- A model that is always-thinking is valid for `reasoning_text`, but is ineligible for any request explicitly requiring `OFF`.
- Qwen single-turn business requests explicitly set `preserve_thinking=false`; no stored `reasoning_content` is required or persisted.

- [ ] **Step 2: Write failing response and secret-safety tests**

```python
@pytest.mark.asyncio
async def test_adapter_uses_final_content_and_discards_reasoning_content():
    adapter = adapter_returning({
        "choices": [{"message": {"reasoning_content": "private chain", "content": "final answer"}}]
    })
    result = await adapter.generate(text_request(), model="model-one")
    assert result.text == "final answer"
    assert "private chain" not in repr(result)


@pytest.mark.asyncio
async def test_empty_final_content_is_switchable_schema_error():
    adapter = adapter_returning({"choices": [{"message": {"reasoning_content": "only thought", "content": ""}}]})
    with pytest.raises(ProviderError) as caught:
        await adapter.generate(text_request(), model="model-one")
    assert caught.value.code == "E_API_EMPTY_CONTENT"
    assert caught.value.switchable is True
```

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_parameter_mapping.py tests/providers/ai/test_openai_chat.py -q`

Expected: imports fail.

- [ ] **Step 4: Extend the provider error model compatibly**

Keep existing constructor call sites valid by appending defaulted fields:

```python
class FailureCategory(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    MODEL = "model"
    SCHEMA = "schema"
    INPUT = "input"
    POLICY = "policy"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    UNKNOWN = "unknown"


@dataclass(eq=False)
class ProviderError(RuntimeError):
    code: str
    retryable: bool
    user_message: str
    request_id: str | None = None
    category: FailureCategory = FailureCategory.UNKNOWN
    switchable: bool = False
    connection_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
```

Map 408/429/5xx, auth, quota, model-unavailable and wire/JSON-schema errors as switchable while shared time remains. Map input size/format, policy rejection, cancellation and an exhausted request deadline as non-switchable. Never catch or wrap `asyncio.CancelledError`; re-raise it immediately. Provider-specific error bodies may be inspected in memory to classify known codes but must not be copied to logs or user messages.

- [ ] **Step 5: Implement request building**

Always send `model` and `messages`. Add `temperature`, `response_format`, reasoning fields and vision content only when the model profile allows them. For Kimi K3 do not send an unsupported thinking-off field; use top-level `reasoning_effort`. For Kimi Code use its probed effort support and do not spoof `User-Agent`. For Qwen single-turn calls set `preserve_thinking=false` when supported and never fabricate historical `reasoning_content`. For custom `standard`, do not claim forced-thinking support and do not send provider-specific fields.

For JSON requests derive JSON Schema from `request.response_model.model_json_schema()` when supported. Otherwise use JSON Object. If neither is supported, send no `response_format`; the business prompt plus local validation remains authoritative.

- [ ] **Step 6: Implement normalized response parsing**

Read only `choices[0].message.content`. Strip text, reject null/empty content, and retain only upstream request ID, provider/model identity and latency. Do not retain the response JSON. Keep `providers/openai_compatible.py` as a deprecated compatibility wrapper around the new task client until every caller is migrated in Task 6.

Before each network call calculate `remaining = deadline_monotonic - time.monotonic()` when a deadline exists. If `remaining <= 0`, raise non-switchable `E_AI_DEADLINE`; otherwise cap connect/read timeout by `remaining`. Cancellation from the caller must cancel the in-flight `httpx` request and must not be transformed into an ordinary provider failure.

- [ ] **Step 7: Run adapter and legacy error tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_parameter_mapping.py tests/providers/ai/test_openai_chat.py tests/providers/test_error_mapping.py -q`

Expected: all tests pass and the secret-safety assertion remains green.

- [ ] **Step 8: Record or commit Task 3**

Commit message when Git exists: `feat: normalize compatible ai requests`.

Otherwise append `Task 3 — request mapping and Chat Completions adapter — PASS` to the progress file.

---

### Task 4: Discover models and probe capabilities without candidate data

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/probes.py`
- Create: `backend/tests/providers/ai/test_probes.py`
- Modify: `backend/src/kerui_recruit/providers/ai/catalog.py`
- Modify: `backend/src/kerui_recruit/providers/connectivity.py`
- Test: `backend/tests/providers/test_connectivity.py`

**Interfaces:**
- Consumes: unsaved/saved `AiConnection`, catalog, `OpenAIChatAdapter`.
- Produces: `DiscoveredModel`, `CapabilityProbe`, `ConnectionProbeReport`, `AiProbeService.discover()`, `probe()`.

- [ ] **Step 1: Write failing discovery and probe tests**

```python
@pytest.mark.asyncio
async def test_discovery_falls_back_to_catalog_when_models_endpoint_is_absent():
    service = probe_service(models_response=httpx.Response(404))
    models = await service.discover(connection("deepseek"))
    assert "deepseek-v4-flash" in {item.model_id for item in models}


@pytest.mark.asyncio
async def test_probe_never_uses_real_resume_text():
    captured = []
    service = probe_service(capture_requests=captured)
    report = await service.probe(connection("deepseek"))
    assert report.text.ok is True
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "测试候选人" in serialized
    assert "真实" not in serialized


@pytest.mark.asyncio
async def test_kimi_code_402_has_membership_action():
    report = await probe_service(status=402).probe(connection("kimi_code"))
    assert report.auth.ok is False
    assert report.auth.error_code == "E_KIMI_MEMBERSHIP"
    assert "会员" in report.auth.suggested_action
```

- [ ] **Step 2: Run tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_probes.py -q`

Expected: import failure.

- [ ] **Step 3: Implement conservative model discovery**

Call `<base_url>/models` with the connection Key. Normalize `data[*].id`. When the endpoint returns 404/405, forbidden, an invalid schema or an empty set, fall back to catalog models and mark `source="catalog"`. Discovery only establishes model existence, not capability.

For Qwen, retain the China legacy default; region/workspace-specific Base URL remains an advanced override and must be paired with a Key from the same region.

Discovery and probe results must update only the connection's runtime capability snapshot; they must not edit the search Embedding/Rerank model selections. `/models` output is authoritative for availability, while built-in catalog data supplies labels and conservative fallbacks when discovery is unavailable.

- [ ] **Step 4: Implement the fixed probe matrix**

Probe in this order and stop when authentication fails:

1. Plain text: fixed message `只回复 OK`.
2. JSON: fixed schema `{"ok": true}` and local Pydantic validation.
3. Reasoning off/on only when the catalog says the model may support a toggle.
4. Vision only when a vision model is selected, using a tiny bundled image containing `API TEST`.

Return each result independently. A connection may be saved with partial capabilities, but it may only enter a role whose probe succeeded.

- [ ] **Step 5: Replace the old single LLM connectivity message**

Keep embedding/rerank/web-search checks unchanged. Generate the LLM summary from the active AI manager when present: `快速模型可用`、`思考模型可用`、`视觉模型不可用` and actionable error codes. The old `调用失败` catch-all is no longer sufficient for generation.

- [ ] **Step 6: Run probe and connectivity tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_probes.py tests/providers/test_connectivity.py -q`

Expected: all tests pass; old offline embedding/rerank assertions are unchanged.

- [ ] **Step 7: Record or commit Task 4**

Commit message when Git exists: `feat: probe ai model capabilities`.

Otherwise append `Task 4 — discovery and capability probes — PASS` to the progress file.

---

### Task 5: Implement bounded failover, circuit breaking and automatic return to primary

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/circuit_breaker.py`
- Create: `backend/src/kerui_recruit/providers/ai/router.py`
- Create: `backend/tests/providers/ai/test_router.py`
- Modify: `backend/src/kerui_recruit/providers/ai/contracts.py`
- Modify: `backend/src/kerui_recruit/providers/errors.py`

**Interfaces:**
- Consumes: ordered connections, role/model profiles, adapters, probe results.
- Produces: `AiProviderRouter.generate()`, `CircuitBreaker`, `ConnectionHealth`, `RouterStatus`.

- [ ] **Step 1: Write the failover decision tests before implementation**

```python
@pytest.mark.asyncio
async def test_429_switches_once_to_secondary():
    primary = adapter_raising(error("E_API_RATE_LIMIT", FailureCategory.RATE_LIMIT, switchable=True))
    secondary = adapter_returning("ok")
    result = await router(primary, secondary).generate(request())
    assert result.text == "ok"
    assert result.fallback_used is True
    assert [item.provider_id for item in result.attempts] == ["deepseek", "qwen"]
    assert primary.calls == 1 and secondary.calls == 1


@pytest.mark.asyncio
async def test_input_error_never_sends_data_to_secondary():
    primary = adapter_raising(error("E_API_INPUT", FailureCategory.INPUT, switchable=False))
    secondary = adapter_returning("should not run")
    with pytest.raises(ProviderError):
        await router(primary, secondary).generate(request())
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_auth_and_quota_failures_use_authorized_backup():
    for category in (FailureCategory.AUTH, FailureCategory.QUOTA):
        result = await router(adapter_raising(error("E", category, True)), adapter_returning("ok")).generate(request())
        assert result.fallback_used is True


@pytest.mark.asyncio
async def test_kimi_code_is_filtered_from_background_and_batch():
    route = router(adapter_raising(error("E_API_BUSY", FailureCategory.SERVER, True)), kimi_code_adapter())
    with pytest.raises(ProviderError):
        await route.generate(request(execution_context=ExecutionContext.BACKGROUND))
    assert route.adapter("kimi_code").calls == 0


@pytest.mark.asyncio
async def test_primary_is_retried_after_cooldown_and_restored():
    clock = FakeClock()
    route = router(flaky_primary(), adapter_returning("backup"), clock=clock)
    assert (await route.generate(request())).provider_id == "qwen"
    clock.advance(seconds=61)
    assert (await route.generate(request())).provider_id == "deepseek"
```

Also cover network/408/5xx/model/schema switching, two failures, no secondary, `Retry-After`, and a hard ceiling of two adapter calls.

Add these current-runtime cases:

```python
@pytest.mark.asyncio
async def test_cancelled_primary_never_calls_secondary():
    secondary = adapter_returning("must not run")
    task = asyncio.create_task(router(cancel_aware_primary(), secondary).generate(request()))
    await primary_started()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_exhausted_shared_deadline_never_starts_secondary():
    secondary = adapter_returning("must not run")
    with pytest.raises(ProviderError) as caught:
        await router(primary_consuming_budget(), secondary).generate(expiring_request())
    assert caught.value.code == "E_AI_DEADLINE"
    assert secondary.calls == 0
```

- [ ] **Step 2: Run router tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_router.py -q`

Expected: modules are absent.

- [ ] **Step 3: Implement deterministic target selection**

For each request:

1. Iterate connections in saved order.
2. Skip disabled connections.
3. Skip a connection when its provider disallows the request execution context.
4. Skip a connection without a successfully probed model for the requested role.
5. Skip an open circuit while a second eligible connection exists.
6. Skip models whose explicit `supported_reasoning_modes` cannot satisfy the request (especially always-thinking models for `OFF`).
7. Return at most two targets.

Do not infer an unconfigured third provider and do not fall back from remote generation to a different remote service absent explicit user configuration.

- [ ] **Step 4: Validate structured output inside each attempt**

When `response_model` is set, normalize markdown fences, call `response_model.model_validate_json(result.text)` before declaring the attempt successful, and place the parsed Pydantic object in `GenerationResult.parsed`. Validation failure becomes switchable `E_API_SCHEMA`; raw generated text must not be logged.

This validation is limited to transport shape and the requested Pydantic schema. Do not move `check_parsed_resume` into the generic router. A Pydantic-valid but business-incomplete resume remains `E_PARSE_INCOMPLETE` in `resumes/pipeline.py` and continues through the existing explicit visual-reparse workflow without an automatic backup charge.

- [ ] **Step 5: Implement circuit cooldowns**

Use an injected monotonic clock for tests. Implement exact defaults from the spec: rate-limit 60 seconds or bounded `Retry-After`, network/server 30 seconds and 120 seconds after three consecutive failures, auth 15 minutes, quota 5 minutes, model unavailable until configuration/discovery/probe changes. `record_success()` closes the circuit and clears consecutive failures.

Protect circuit mutation and half-open admission for concurrent API/worker/scheduler calls. At most one request may own a connection's half-open probe at a time; other requests use an eligible backup or return the typed unavailable error.

- [ ] **Step 6: Add a sanitized final error**

When both targets fail, raise `ProviderError(code="E_AI_ALL_PROVIDERS_FAILED", retryable=True, user_message="主服务和备用服务当前均不可用")`. Attach attempt diagnostics as typed metadata, not by concatenating raw errors. The FastAPI error response added later may expose provider labels and safe error codes only.

Before starting the second attempt, re-check task cancellation and the shared absolute deadline. The fallback call is forbidden when either condition prevents useful completion, even if the primary error category would normally be switchable.

- [ ] **Step 7: Run all routing tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_router.py tests/providers/ai/test_openai_chat.py -q`

Expected: all tests pass and maximum remote-call assertions equal two.

- [ ] **Step 8: Record or commit Task 5**

Commit message when Git exists: `feat: add bounded ai provider failover`.

Otherwise append `Task 5 — failover and circuit breaker — PASS` to the progress file.

---

### Task 6: Route every generation caller through task-aware clients

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/task_client.py`
- Create: `backend/src/kerui_recruit/providers/generation_tasks.py`
- Modify: `backend/src/kerui_recruit/providers/deepseek.py`
- Modify: `backend/src/kerui_recruit/providers/openai_compatible.py`
- Modify: `backend/src/kerui_recruit/providers/ocr.py`
- Modify: `backend/src/kerui_recruit/providers/vision_parse.py`
- Modify: `backend/src/kerui_recruit/providers/leads.py`
- Modify: `backend/src/kerui_recruit/mail/resume_gate.py`
- Modify: `backend/src/kerui_recruit/org/import_parser.py`
- Modify: `backend/src/kerui_recruit/bd_agent/planner.py`
- Modify: `backend/src/kerui_recruit/bd_agent/synthesis.py`
- Modify: `backend/src/kerui_recruit/search/rewrite.py`
- Modify: `backend/src/kerui_recruit/resumes/profile.py`
- Modify: `backend/src/kerui_recruit/jd/profile.py`
- Modify: corresponding tests under `backend/tests/providers`, `backend/tests/bd_agent`, `backend/tests/search`, `backend/tests/org`, `backend/tests/mail`, `backend/tests/resumes`, `backend/tests/jd`

**Interfaces:**
- Consumes: any `GenerationClient` implementation.
- Produces: `TaskGenerationClient.complete_json()`, `complete_text()`, `AiResumeParser`, `AiJdParser`, routed vision/OCR clients.

- [ ] **Step 1: Write task-policy tests**

```python
@pytest.mark.asyncio
async def test_resume_parser_requests_fast_nonthinking_background_json():
    gateway = CapturingGenerationClient(parsed=ParsedResume(name="张三"))
    await AiResumeParser(TaskGenerationClient(
        gateway, task_kind=TaskKind.RESUME_PARSE,
        role=ModelRole.FAST_TEXT, execution_context=ExecutionContext.BACKGROUND,
    )).parse_resume("张三简历")
    request = gateway.requests[0]
    assert (request.role, request.reasoning, request.output_mode) == (
        ModelRole.FAST_TEXT, ReasoningMode.OFF, OutputMode.JSON,
    )


@pytest.mark.asyncio
async def test_bd_planner_requests_reasoning_interactive_json():
    gateway = CapturingGenerationClient(parsed=QueryPlan(queries=[]))
    await QueryPlanner(TaskGenerationClient(
        gateway, task_kind=TaskKind.BD_PLAN,
        role=ModelRole.REASONING_TEXT, execution_context=ExecutionContext.INTERACTIVE,
    )).plan("寻找候选人")
    assert gateway.requests[0].reasoning is ReasoningMode.REQUIRED
```

- [ ] **Step 2: Run focused caller tests and confirm failure**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/test_deepseek.py tests/providers/test_deepseek_jd.py tests/providers/test_leads.py tests/providers/test_ocr.py -q`

Expected: the new task client/classes are not available.

- [ ] **Step 3: Implement the compatibility facade**

`TaskGenerationClient` stores the gateway, task kind, role, execution context and reasoning defaults. Its public methods keep the signatures callers already use:

```python
async def complete_json(
    self,
    messages: list[dict[str, Any]],
    response_model: type[ResultModel],
    temperature: float | None = None,
    *,
    execution_context: ExecutionContext | None = None,
    deadline_monotonic: float | None = None,
) -> ResultModel: ...

async def complete_text(
    self,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    *,
    execution_context: ExecutionContext | None = None,
    deadline_monotonic: float | None = None,
) -> str: ...
```

The new keyword-only arguments preserve every current positional caller. `execution_context=None` uses the client's default; explicit callers may distinguish manual regeneration from backfill. `complete_json` must return `GenerationResult.parsed` and assert it is the requested model type. Expose a read-only `cache_identity` that changes with manager configuration revision, task kind, role and resolved route model IDs, and never contains a Key. `providers/openai_compatible.py` becomes a compatibility import/wrapper and must no longer send HTTP itself.

- [ ] **Step 4: Move supplier-neutral parsing classes out of `deepseek.py`**

Move the implementation-time prompts without changing their content to `generation_tasks.py`. The current prompts include `direction` and other fields added after the first plan; copy from the current source, not from this document. Rename the implementations `AiResumeParser` and `AiJdParser` and update internal imports/tests atomically. Do **not** use `DeepSeekResumeParser = AiResumeParser` if constructor signatures differ. Either remove the provider-specific internal name in the same task, or provide a deprecated wrapper with the exact new `TaskGenerationClient` constructor; no wrapper may reconstruct a direct HTTP client or silently preserve the old API-key constructor.

- [ ] **Step 5: Assign a role and context to every current caller**

Use this exact mapping:

| Caller | Task kind | Role | Context | Reasoning |
| --- | --- | --- | --- | --- |
| Resume parser | `resume_parse` | `fast_text` | `background` | off |
| JD parser/split | `jd_parse` | `fast_text` | `background` | off |
| Search rewrite | `query_rewrite` | `fast_text` | `interactive` | off |
| Candidate/JD profile manual regenerate | profile kind | `fast_text` | `interactive` | off |
| Candidate/JD profile automatic backfill | profile kind | `fast_text` | `batch` | off |
| Org import/revise | `org_parse` | `fast_text` | `interactive` | off |
| Lead extraction | `lead_extract` | `fast_text` | `interactive` | off |
| Mail resume gate | `mail_resume_gate` | `fast_text` | `background` | off |
| BD planner | `bd_plan` | `reasoning_text` | `interactive` | required/high |
| BD synthesis | `bd_synthesis` | `reasoning_text` | `interactive` | required/high |
| OCR | `ocr` | `vision` | `background` | off |
| Vision structured parser | `vision_parse` | `vision` | `background` | off |

This mapping intentionally prevents Kimi Code from receiving resume/JD parsing, mail ingestion, profile backfill and visual background work. `CandidateProfileGenerator.generate(data, instruction=None, previous=None)` and `JdProfileGenerator.generate(...)` must retain their current signatures and previous-profile incremental prompt semantics; add only a keyword-only execution-context override if necessary. `BackfillService.regenerate_*` passes `INTERACTIVE`, while `_backfill_profiles` passes `BATCH`.

- [ ] **Step 6: Remove duplicate direct HTTP clients**

`ResumeGate`, `DeepSeekLeadExtractor`, `OpenAICompatibleOCRProvider`, `VisionStructuredParser` and `DeepSeekOrgImportParser` must consume `TaskGenerationClient` instead of owning API Key/Base URL/model fields. Preserve their public business methods and existing prompt semantics.

Remove any log of raw vision structured output. Validation failures log only task kind, provider/model diagnostic and error code.

Update `SemanticQueryRewriter` without changing its filtering behavior, TTL, LRU size or fallback semantics:

- cache key becomes `<task_client.cache_identity>|<LEXICON_VERSION>|<normalized query>` instead of reading one static `.model`;
- `rewrite()` accepts an optional keyword-only absolute deadline and forwards it to `complete_json`;
- `HybridSearchService._maybe_rewrite` passes its existing `rewrite_deadline` while retaining the outer `_provider` cancellation guard;
- update the stale lexicon-version test so it always monkeypatches to a value different from the current constant, then add a manager revision/cache-identity miss test.

Keep `check_parsed_resume(parsed, source_text)` after provider schema parsing in `resumes/pipeline.py`. Add regression coverage proving that `E_PARSE_INCOMPLETE`, failed-item backfill (`REPARSE_FAILED`), `use_vision=True`, manual overrides and `direction` survive the migration unchanged.

- [ ] **Step 7: Run all affected business tests**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers tests/bd_agent tests/search tests/org tests/mail tests/resumes tests/jd tests/backfill -q`

If a listed directory does not exist, run the existing matching test files returned by `rg --files backend/tests` and record the exact command. Expected: all newly added/modified affected tests pass and `rg -n "http_client.post|client.post" backend/src/kerui_recruit/providers/leads.py backend/src/kerui_recruit/mail/resume_gate.py backend/src/kerui_recruit/org/import_parser.py backend/src/kerui_recruit/providers/vision_parse.py` finds no direct generation HTTP call. If the broader `tests/search` set reproduces a preflight timing failure, record the exact before/after comparison instead of weakening timeouts or assertions.

- [ ] **Step 8: Record or commit Task 6**

Commit message when Git exists: `refactor: route generation tasks through ai gateway`.

Otherwise append `Task 6 — all generation callers use task-aware gateway — PASS` to the progress file.

---

### Task 7: Add the provider manager, runtime hot reload and preserve search providers

**Files:**
- Create: `backend/src/kerui_recruit/providers/ai/manager.py`
- Create: `backend/tests/providers/ai/test_manager.py`
- Modify: `backend/src/kerui_recruit/providers/factory.py`
- Modify: `backend/src/kerui_recruit/runtime.py`
- Modify: `backend/src/kerui_recruit/api/services.py`
- Modify: `backend/src/kerui_recruit/providers/connectivity.py`
- Modify: `backend/tests/providers/test_factory.py`
- Modify: `backend/tests/test_runtime.py`

**Interfaces:**
- Consumes: catalog, config store, probe service, router, task clients.
- Produces: `AiProviderManager.start()`, `generate()`, `update_config()`, `status()`, `close()` and stable task-client factory.

- [ ] **Step 1: Write failing manager tests**

```python
@pytest.mark.asyncio
async def test_saved_config_is_visible_to_next_request_without_runtime_rebuild():
    manager = make_manager(config=one_connection("deepseek"))
    client = manager.task_client(TaskKind.QUERY_REWRITE)
    assert (await client.complete_text([{"role": "user", "content": "a"}])) == "deepseek-a"
    await manager.update_config(two_connections(primary="qwen", secondary="deepseek"))
    assert (await client.complete_text([{"role": "user", "content": "b"}])) == "qwen-b"


def test_generation_change_does_not_change_embedding_or_reranker(tmp_path):
    before = build_providers(search_settings(tmp_path), ai_manager=manager("deepseek"))
    manager("deepseek").replace_for_test("qwen")
    after = build_providers(search_settings(tmp_path), ai_manager=manager("qwen"))
    assert type(before.embedding) is type(after.embedding)
    assert type(before.reranker) is type(after.reranker)
    assert before.vector_dimension == after.vector_dimension
```

- [ ] **Step 2: Run focused tests and confirm red state**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_manager.py tests/providers/test_factory.py -q`

Expected: manager is missing.

- [ ] **Step 3: Implement immutable snapshot replacement**

Define an immutable `AiProviderSnapshot` containing revision, resolved connections, role routes and adapters. `generate()` reads one snapshot reference at request start. `update_config()` validates and probes a candidate, saves it, constructs the new snapshot, then swaps the reference under `asyncio.Lock`. In-flight requests retain their old local snapshot. The manager exposes a secret-free task `cache_identity`; it changes on successful config/catalog/model-route replacement so search-rewrite caches cannot retain a stale model identity.

Use one manager-owned `httpx.AsyncClient`. Do not close it during ordinary snapshot replacement because adapters share it; close it once in application lifespan shutdown.

- [ ] **Step 4: Build the manager before business services**

In `runtime.py`, construct `EncryptionService`, `CatalogService`, `AiConfigStore`, migration, shared HTTP client and `AiProviderManager` before `build_providers()`. Add the manager to `AppServices` and close it in `lifespan` after worker/scheduler cancellation.

- [ ] **Step 5: Keep local fallback dynamic**

Even when no AI connection exists at startup, construct routed parser/JD/task clients that consult the manager at call time. If no eligible remote route exists, resume/JD parsing uses the existing deterministic local parser. Vision requests without an eligible route return a clear `E_AI_VISION_NOT_CONFIGURED` rather than calling an arbitrary text model.

- [ ] **Step 6: Limit `providers/factory.py` changes to generation wiring**

Keep current SiliconFlow/local Embedding and Rerank construction, vector dimension and search HTTP lifecycle unchanged. Remove only static generation selection from factory. `ProviderBundle` may retain compatibility fields, but generation parser/OCR/vision members must be routed proxies backed by the manager.

- [ ] **Step 7: Eliminate duplicated text-route resolution from `runtime.py`**

Delete the `text_* > deepseek_* > siliconflow_*` block and direct `OpenAICompatibleClient` construction. Create task clients through `manager.task_client(...)` for rewriter, lead extractor, profiles, org import, BD Agent and resume gate. Legacy precedence is handled once by config migration. Preserve current construction of `SemanticQueryRewriter`, its injection into `HybridSearchService`, and all existing deadline/cancellation wiring.

- [ ] **Step 8: Run runtime and search regression**

Run: `Set-Location backend; py -3.12 -m pytest tests/providers/ai/test_manager.py tests/providers/test_factory.py tests/test_runtime.py tests/api/test_search_consistency.py tests/api/test_index_status.py -q`

Expected: hot reload test passes; search consistency/index tests remain unchanged; no generation setting alters vector dimension/model metadata. Add an assertion that a config revision change alters `SemanticQueryRewriter`'s cache identity, while circuit-only fallback does not expose secrets in the key.

- [ ] **Step 9: Record or commit Task 7**

Commit message when Git exists: `feat: hot reload ai provider routes`.

Otherwise append `Task 7 — manager and runtime hot reload — PASS` to the progress file.

---

### Task 8: Expose safe AI catalog, probe, configuration and status APIs

**Files:**
- Create: `backend/src/kerui_recruit/api/ai_settings.py`
- Create: `backend/tests/api/test_ai_settings.py`
- Modify: `backend/src/kerui_recruit/api/services.py`
- Modify: `backend/src/kerui_recruit/api/settings.py`
- Modify: `backend/src/kerui_recruit/main.py`

**Interfaces:**
- Consumes: `AiProviderManager`, `AiConfigStore`, `CatalogService`, `AiProbeService`.
- Produces: `/api/ai/catalog`, `/api/ai/catalog/refresh`, `/api/ai/config`, `/api/ai/probe`, `/api/ai/status`.

- [ ] **Step 1: Write failing API contract and secret tests**

```python
def test_catalog_defaults_to_deepseek(client):
    payload = client.get("/api/ai/catalog", headers=session()).json()
    assert payload["default_provider_id"] == "deepseek"
    assert [item["provider_id"] for item in payload["providers"]][:2] == ["deepseek", "kimi_open"]


def test_save_two_connections_never_returns_keys(client):
    response = client.put("/api/ai/config", headers=session(), json={
        "connections": [
            {"provider_id": "deepseek", "display_name": "DeepSeek", "api_key": "ds-secret"},
            {"provider_id": "qwen", "display_name": "通义千问", "api_key": "qw-secret"},
        ]
    })
    assert response.status_code == 200
    assert "ds-secret" not in response.text and "qw-secret" not in response.text
    assert response.json()["protection_level"] == "dual"


def test_more_than_two_connections_is_rejected(client):
    response = client.put("/api/ai/config", headers=session(), json={"connections": [payload("a"), payload("b"), payload("c")]})
    assert response.status_code == 422


def test_probe_uses_unsaved_key_without_echoing_it(client):
    response = client.post("/api/ai/probe", headers=session(), json={
        "provider_id": "deepseek", "api_key": "temporary-secret",
    })
    assert response.status_code == 200
    assert "temporary-secret" not in response.text
```

- [ ] **Step 2: Run tests and confirm 404 responses**

Run: `Set-Location backend; py -3.12 -m pytest tests/api/test_ai_settings.py -q`

Expected: new routes return 404.

- [ ] **Step 3: Implement request and response models**

All request models use `extra="forbid"`. `ConnectionUpdate` fields are `connection_id`, `provider_id`, `display_name`, optional `api_key`, `clear_api_key=False`, optional `base_url_override`, `models`, and `enabled=True`. Missing `api_key` preserves an existing secret only when `connection_id` matches; a new connection without a Key is rejected.

Response connections contain `masked_api_key`, `has_api_key`, provider and model data only. They never contain `api_key` or `encrypted_api_key` fields.

- [ ] **Step 4: Implement endpoints and stable status codes**

- `GET /api/ai/catalog`: 200.
- `POST /api/ai/catalog/refresh`: 200 with `updated/cached/builtin/disabled` state.
- `GET /api/ai/config`: 200 with `protection_level` equal to `none/single/dual`.
- `PUT /api/ai/config`: 200 only after validation, save and atomic manager swap; a fully unusable configuration returns 422 with per-role safe details.
- `POST /api/ai/probe`: 200 even when upstream checks fail; each capability carries `ok/error_code/suggested_action`. Malformed local input returns 422.
- `GET /api/ai/status`: 200 with circuit state and last fallback summary.

- [ ] **Step 5: Keep old settings API compatible but stop new AI writes**

`GET /api/settings/vendors` remains for an old frontend and adds `deprecated: true`. `PUT /api/settings` continues mail/search settings. When a request supplies a newly changed legacy generation field, return `E_AI_SETTINGS_MOVED`; an unchanged masked value from an older page may be ignored. Do not block legacy embedding/rerank fields during this compatibility cycle. Preserve the currently added search/operator/rewrite/index settings and do not remove `reparse-failed`, `direction`, query-plan or match-related API types while editing shared service models.

- [ ] **Step 6: Register the router and test error sanitation**

Add the router to `main.py`. Extend the `ProviderError` handler so `E_AI_ALL_PROVIDERS_FAILED` may include sanitized attempts under `details`, with only provider label, model, error code and latency. Test that upstream response bodies, prompts, Key fragments and reasoning never appear.

- [ ] **Step 7: Run API and auth regression**

Run: `Set-Location backend; py -3.12 -m pytest tests/api/test_ai_settings.py tests/api/test_mail_settings.py tests/api/test_local_api.py -q`

Expected: AI endpoints pass local-session authentication and mail settings still work.

- [ ] **Step 8: Record or commit Task 8**

Commit message when Git exists: `feat: expose dual ai configuration api`.

Otherwise append `Task 8 — safe AI settings API — PASS` to the progress file.

---

### Task 9: Add typed frontend state and API methods before changing the page

**Files:**
- Create: `desktop/src/ai/types.ts`
- Modify: `desktop/src/api/client.ts`
- Modify: `desktop/src/App.tsx`
- Modify: `desktop/tests/api-client.test.ts`
- Modify: `desktop/tests/App.test.tsx`

**Interfaces:**
- Consumes: Task 8 JSON API.
- Produces: `AiCatalog`, `AiConfig`, `AiConnectionView`, `ConnectionProbeReport`, `AiStatus`, and `RecruitmentApi` methods.

- [ ] **Step 1: Write failing client serialization tests**

```typescript
test("sends at most two ai connections and never puts keys in URLs", async () => {
  const requests: Request[] = [];
  const client = recordingClient(requests);
  await client.updateAiConfig({ connections: [
    { provider_id: "deepseek", display_name: "DeepSeek", api_key: "ds-secret", models: {}, enabled: true },
    { provider_id: "qwen", display_name: "通义千问", api_key: "qw-secret", models: {}, enabled: true },
  ] });
  expect(new URL(requests[0].url).pathname).toBe("/api/ai/config");
  expect(requests[0].url).not.toContain("secret");
  expect(await requests[0].json()).toHaveProperty("connections", expect.any(Array));
});

test("probe key is sent only in POST body", async () => {
  const requests: Request[] = [];
  await recordingClient(requests).probeAiConnection({ provider_id: "deepseek", api_key: "probe-secret" });
  expect(requests[0].method).toBe("POST");
  expect(requests[0].url).not.toContain("probe-secret");
});
```

- [ ] **Step 2: Run client tests and confirm missing methods**

Run: `Set-Location desktop; npm test -- --run tests/api-client.test.ts`

Expected: TypeScript reports missing AI API methods.

- [ ] **Step 3: Define frontend types**

Use exact unions:

```typescript
export type AiProviderId = "deepseek" | "kimi_open" | "kimi_code" | "qwen" | "zhipu" | "siliconflow" | "custom_openai";
export type ModelRole = "fast_text" | "reasoning_text" | "vision";
export type ProtectionLevel = "none" | "single" | "dual";
export type CircuitState = "closed" | "open" | "half_open";
```

Define connection view/update separately so API responses cannot accidentally be assigned to a type containing plaintext `api_key`.

- [ ] **Step 4: Add typed API client methods**

Add `getAiCatalog()`, `refreshAiCatalog()`, `getAiConfig()`, `updateAiConfig()`, `probeAiConnection()` and `getAiStatus()` using the existing authenticated `request<T>()` helper. Never persist probe input in module-level state.

- [ ] **Step 5: Replace App-level legacy AI state**

Remove `vendors`, generation-specific fields from the AI form state, `providerChecks` and the restart-after-save message. Add `aiCatalog`, `aiConfig`, `aiStatus`, `aiBusy` and `aiMessage`. Keep `AppSettings` fields needed by mail, web search, embedding and rerank unchanged. Do not regress the current `CandidateSearchOptions`, `query_plan`, keyword operator, rewrite preference, `direction`, `reparse-failed` or match request/response types while editing the now-large shared `App.tsx` and `client.ts` files.

Update the `fakeApi()` implementation in `App.test.tsx` with realistic catalog/config/status responses and new methods.

- [ ] **Step 6: Run type and App regression**

Run: `Set-Location desktop; npm test -- --run tests/api-client.test.ts tests/App.test.tsx`

Expected: tests pass; no UI change is required yet beyond compiling the new state.

- [ ] **Step 7: Record or commit Task 9**

Commit message when Git exists: `refactor: add typed ai settings state`.

Otherwise append `Task 9 — typed frontend AI state — PASS` to the progress file.

---

### Task 10: Replace the complex model form with a DeepSeek-first dual-service wizard

**Files:**
- Create: `desktop/src/ai/AiServicesPanel.tsx`
- Create: `desktop/src/ai/ConnectionWizard.tsx`
- Create: `desktop/src/ai/AdvancedAiSettings.tsx`
- Create: `desktop/tests/AiServicesPanel.test.tsx`
- Create: `desktop/tests/ConnectionWizard.test.tsx`
- Create: `desktop/tests/AdvancedAiSettings.test.tsx`
- Modify: `desktop/src/pages/SettingsPage.tsx`
- Modify: `desktop/src/App.tsx`
- Modify: `desktop/src/styles.css`
- Modify: `desktop/tests/App.test.tsx`

**Interfaces:**
- Consumes: Task 9 typed state and callbacks.
- Produces: simple primary/backup configuration experience embedded in the existing Settings page.

- [ ] **Step 1: Write failing default-view and single-service tests**

```typescript
test("shows DeepSeek as recommended without technical fields", () => {
  render(<AiServicesPanel {...emptyProps} />);
  expect(screen.getByText("DeepSeek")).toBeVisible();
  expect(screen.getByText("推荐主服务")).toBeVisible();
  expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
  expect(screen.queryByText("reasoning_effort")).not.toBeInTheDocument();
});

test("a single service works but recommends backup protection", () => {
  render(<AiServicesPanel {...singleDeepSeekProps} />);
  expect(screen.getByText("单服务可用")).toBeVisible();
  expect(screen.getByRole("button", { name: "添加备用 AI 服务" })).toBeVisible();
  expect(screen.getByText("当前没有备用保护")).toBeVisible();
});
```

- [ ] **Step 2: Write failing two-service and Kimi policy tests**

```typescript
test("two providers show dual protection and explicit order", () => {
  render(<AiServicesPanel {...dualProps("deepseek", "qwen")} />);
  expect(screen.getByText("双服务保护已开启")).toBeVisible();
  expect(screen.getByText("主服务")).toBeVisible();
  expect(screen.getByText("备用服务")).toBeVisible();
});

test("Kimi Code requires acknowledgement and is not recommended as automatic backup", async () => {
  const user = userEvent.setup();
  render(<ConnectionWizard {...wizardProps} slot="secondary" />);
  await user.click(screen.getByRole("button", { name: /Kimi Code 订阅/ }));
  expect(screen.getByText(/仅限个人交互式使用/)).toBeVisible();
  expect(screen.getByRole("button", { name: "检测并继续" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /我已了解使用范围/ }));
  expect(screen.getByRole("button", { name: "检测并继续" })).toBeEnabled();
});
```

The Kimi Code copy must also state that it is intended for coding/IDE-style personal use and that product integration should use Kimi Open Platform. Keep it after SiliconFlow in the default provider list, behind an advanced/warning affordance, and never label it as the recommended automatic backup.

- [ ] **Step 3: Run component tests and confirm missing imports**

Run: `Set-Location desktop; npm test -- --run tests/AiServicesPanel.test.tsx tests/ConnectionWizard.test.tsx tests/AdvancedAiSettings.test.tsx`

Expected: component imports fail.

- [ ] **Step 4: Implement the AI service overview**

Render status copy exactly:

- zero usable connections: `未配置 AI 服务`.
- one usable connection: `单服务可用` and `当前没有备用保护`.
- two usable different-provider connections: `双服务保护已开启`.
- two usable same-provider connections: `已配置两个 Key，但同一供应商故障时可能同时不可用`.
- recent fallback: `<主供应商> 当前不可用，本次已由 <备用供应商> 完成`.

Provider cards show friendly name, `主服务/备用服务`, capabilities and last safe error. Hide model IDs and Base URL until advanced settings opens.

- [ ] **Step 5: Implement the four-step wizard**

Step 1 provider order: DeepSeek, Kimi 开放平台, 通义千问, 智谱 GLM, 硅基流动, Kimi Code 订阅, 自定义 OpenAI 兼容。Step 2 Key. Step 3 call `probeAiConnection`. Step 4 show role allocation and save.

If the primary slot is empty, preselect DeepSeek. When the primary is DeepSeek, opening the secondary wizard must not preselect DeepSeek; show different-provider choices first. Saving clears the plaintext Key from React state immediately.

- [ ] **Step 6: Implement advanced settings**

Advanced UI allows reorder, model selection from discovered models, custom model ID, custom Base URL and compatibility style. Production custom URLs accept HTTPS only. Changing provider or Base URL invalidates prior probe results and requires retest before enabling the connection.

- [ ] **Step 7: Recompose the existing Settings page without disturbing other sections**

Replace only lines representing the old “模型 API 配置” form with `AiServicesPanel`. Keep startup checks, Embedding/Rerank/Search settings, mail/reminders, backups, migration, failed-item reparse controls and index sync sections. Preserve all recently added search/match controls elsewhere in the application. If SiliconFlow Key still configures Embedding/Rerank, place that field under “检索服务（高级）”, not inside the new generation wizard.

At widths below 720px, stack provider cards and action buttons. Preserve keyboard focus, form labels, alert semantics and existing button variants.

- [ ] **Step 8: Update App behavior tests**

Replace the obsolete assertion `API 配置已保存，重启应用后生效` with `AI 配置已保存并生效`. Add an App test where status reports a fallback and the page displays it without a blocking dialog.

- [ ] **Step 9: Run frontend tests and production build**

Run: `Set-Location desktop; npm test -- --run tests/AiServicesPanel.test.tsx tests/ConnectionWizard.test.tsx tests/AdvancedAiSettings.test.tsx tests/App.test.tsx`

Run: `Set-Location desktop; npm run build`

Expected: component/App tests and TypeScript/Vite build pass.

- [ ] **Step 10: Record or commit Task 10**

Commit message when Git exists: `feat: add DeepSeek-first dual ai setup`.

Otherwise append `Task 10 — dual-service settings UI — PASS` to the progress file.

---

### Task 11: Package the catalog and prove failover end to end

**Files:**
- Create: `desktop/tests/ai-settings.spec.ts`
- Create: `docs/verification/dual-ai-provider-acceptance.md`
- Modify: `kerui-recruit-sidecar.spec`
- Modify: `backend/tests/test_sidecar.py`
- Modify: `使用说明.md`

**Interfaces:**
- Consumes: the completed backend, frontend and built-in catalog.
- Produces: packaged resource, E2E proof, user instructions and final release evidence.

- [ ] **Step 1: Add a packaged-resource test**

Verify that a frozen/PyInstaller runtime can resolve `provider_catalog.builtin.json` without relying on the source tree current directory. Add the JSON to `kerui-recruit-sidecar.spec` datas without removing its current `jieba` data/submodule collection or other hidden imports. The sidecar offline smoke test must load all seven provider entries from the bundled resource.

- [ ] **Step 2: Write the nontechnical happy-path E2E test**

```typescript
test("configures DeepSeek and a different backup", async ({ page }) => {
  await page.getByText("设置").click();
  await page.getByRole("button", { name: "添加 AI 服务" }).click();
  await page.getByRole("button", { name: /DeepSeek/ }).click();
  await page.getByLabel("API Key").fill("e2e-deepseek-key");
  await page.getByRole("button", { name: "检测并继续" }).click();
  await page.getByRole("button", { name: "保存为主服务" }).click();
  await page.getByRole("button", { name: "添加备用 AI 服务" }).click();
  await page.getByRole("button", { name: /通义千问/ }).click();
  await page.getByLabel("API Key").fill("e2e-qwen-key");
  await page.getByRole("button", { name: "检测并继续" }).click();
  await page.getByRole("button", { name: "保存为备用服务" }).click();
  await expect(page.getByText("双服务保护已开启")).toBeVisible();
});
```

Mock upstream providers at the backend `httpx` transport boundary; no real Key or Internet is allowed in automated tests.

- [ ] **Step 3: Add failover and non-failover E2E cases**

Case A: DeepSeek returns 429, Qwen returns valid JSON; expect the business action succeeds and UI reports fallback. Assert exactly two upstream requests.

Case B: DeepSeek returns input-format/policy rejection; expect the business action fails and Qwen receives no request.

Case C: both return 503; expect `主服务和备用服务当前均不可用` and safe provider diagnostics.

Case D: DeepSeek recovers after injected cooldown; the following request uses DeepSeek again.

Case E: Kimi Code is configured second; a background resume/mail task never calls it.

Case F: cancel an in-flight primary request; expect `CancelledError` propagation and zero backup requests.

Case G: the primary consumes the shared search-rewrite deadline; expect zero backup requests and the existing `rewrite_status="unavailable"` fallback to the original query.

Case H: the primary returns Pydantic-valid but business-incomplete resume data; expect the existing `E_PARSE_INCOMPLETE`/visual-reparse behavior, not a generic automatic backup call.

Case I: save a different model route; expect the next identical semantic query to miss the old rewrite cache through changed `cache_identity`.

- [ ] **Step 4: Run the full automated suite**

Run: `Set-Location backend; py -3.12 -m pytest -q`

Run: `Set-Location desktop; npm test -- --run`

Run: `Set-Location desktop; npx playwright test tests/ai-settings.spec.ts`

Expected: all new AI-focused and modified focused suites pass. Record exact pass counts, skipped tests and durations. Compare the full backend failure names with the preflight baseline: no new failure is allowed. If the same inherited timing-sensitive search tests reproduce, record them verbatim in the acceptance report and do not claim the full suite is green; do not relax deadlines, increase test timeouts, skip or delete tests to manufacture a pass.

- [ ] **Step 5: Build and smoke-test the sidecar offline**

Run: `py -3.12 -m PyInstaller --noconfirm kerui-recruit-sidecar.spec`

Start the packaged sidecar with a temporary data root and no provider environment variables. Call `GET /api/ai/catalog` with the launch token. Expected: DeepSeek is default, all seven entries are returned, and no network call is needed.

- [ ] **Step 6: Perform a secret and direct-call scan**

Run:

```powershell
rg -n "api_key.*(print|log)|logger.*api_key|reasoning_content.*(print|log)|sk-[A-Za-z0-9]{12,}" backend/src desktop/src
rg -n "chat/completions" backend/src/kerui_recruit --glob '!providers/ai/openai_chat.py'
```

Expected: no secret/reasoning logging. The second command may find official URL documentation strings or tests only; every live generation request must be centralized in `providers/ai/openai_chat.py`.

- [ ] **Step 7: Update user documentation**

`使用说明.md` must explain:

- DeepSeek is recommended but not mandatory.
- One Key works; two different providers are recommended.
- Primary/backup behavior and which failures trigger switching.
- Automatic switching can result in billing from the backup provider.
- Kimi Open and Kimi Code are different systems; keys cannot be mixed.
- Kimi Code is restricted and not used for automatic/background/batch work.
- How to replace an expired Key, test a connection and inspect recent fallback status.
- Kimi Open currently separates a switchable-thinking fast model from an always-thinking K3 route; model availability is discovered and can change.
- Embedding/Rerank/search index (current baseline Schema v8/chunk v5) are separate and were not changed by this feature.

- [ ] **Step 8: Write acceptance evidence**

Create `docs/verification/dual-ai-provider-acceptance.md` with:

- source revision or file snapshot date;
- exact preflight baseline commands, pass/fail counts and failing test node IDs;
- backend/frontend/E2E pass counts;
- packaged executable path and SHA-256;
- built-in catalog version;
- offline catalog result;
- 429 failover request count;
- non-switchable error request count;
- Kimi Code background-block result;
- cancellation/deadline zero-backup results;
- `E_PARSE_INCOMPLETE`/visual-reparse preservation result;
- cache-identity invalidation result;
- secret scan result;
- any observed official model-name change applied to the built-in catalog.

- [ ] **Step 9: Record or commit Task 11**

If Git exists:

```powershell
git add desktop/tests/ai-settings.spec.ts docs/verification/dual-ai-provider-acceptance.md kerui-recruit-sidecar.spec backend/tests/test_sidecar.py 使用说明.md
git commit -m "test: verify dual ai provider failover"
```

Otherwise append `Task 11 — package and end-to-end acceptance — PASS` plus all pass counts and artifact paths to the progress file.

---

## Trae Execution Order and Checkpoints

Trae must execute tasks in order. Do not start the frontend UI before Tasks 1–8 are green because the wizard depends on stable backend response shapes.

Checkpoints:

- After Task 2: new config can be saved/migrated but is not yet used by business requests.
- After Task 5: adapter, error classification, bounded failover and circuit breaker are independently proven.
- After Task 7: every runtime generation request uses hot-reloadable main/backup routing; search behavior and Schema v8/chunk v5 remain unchanged.
- After Task 10: nontechnical settings experience is complete.
- After Task 11: feature is releasable.

At every checkpoint, Trae should re-run `rg -n "OpenAICompatibleClient|chat/completions|deepseek_api_key|text_api_key|vision_api_key" backend/src/kerui_recruit desktop/src` and explain every remaining live match. Legacy fields may remain for migration; duplicated generation routing may not. Also run `rg -n "INDEX_SCHEMA_VERSION|INDEX_CHUNK_VERSION|query_plan|direction|REPARSE_FAILED|E_PARSE_INCOMPLETE" backend/src backend/tests desktop/src` and confirm the current non-AI contracts were preserved.

## Rollback Strategy

- Keep old settings fields for one compatibility cycle; do not delete them during this plan.
- Before Task 7, the new manager is not wired into runtime and can be removed without touching search.
- After Task 7, add one internal compatibility switch allowing runtime construction to use the legacy generation bundle during emergency rollback; default it off and do not expose it in the normal UI.
- Configuration writes keep `.bak`; a failed new-config load falls back to the last valid file, then legacy migration/local parsing.
- Circuit-breaker state is in memory only, so restarting clears stale health state without changing user configuration.
- No database or index rollback is needed because this plan does not migrate recruitment or search data.

## Definition of Done

- A fresh user can configure DeepSeek with only an API Key and use AI immediately without restart.
- A user can stop after one working connection and receives a clear lack-of-backup warning.
- A user can add a second different provider and sees dual protection.
- Network errors, 429, provider busy, authentication failure, quota exhaustion, retired models and wire/JSON-schema-invalid model output switch at most once to the authorized backup while the shared deadline still has time.
- Input/policy/cancellation/exhausted-deadline errors never call the backup; `E_PARSE_INCOMPLETE` remains in the explicit visual-reparse workflow rather than generic failover.
- The circuit breaker skips an unhealthy primary during cooldown and automatically restores it after a successful half-open call.
- Fast, reasoning and vision requests use capability-appropriate models and provider-specific supported parameters; always-thinking models never receive `reasoning=off` work.
- Kimi Open and Kimi Code remain separated; Kimi Code never serves background or batch requests.
- All generation call sites use the central gateway; no duplicate direct Chat Completions client remains.
- AI configuration hot reloads, changes the secret-free rewrite `cache_identity`, and leaves Embedding, Rerank, Schema v8/chunk v5, query plans, direction fields and retrieval behavior unchanged.
- Candidate/JD profile manual incremental regeneration and batch contexts remain distinct; `instruction` and `previous` semantics are preserved.
- Secrets and reasoning content are absent from logs, responses and saved plain text.
- AI-focused backend, frontend, E2E and packaged offline acceptance passes; the full-suite report proves no new failures against the recorded preflight and names any inherited failures honestly.
