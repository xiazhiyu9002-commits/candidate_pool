# 双 AI 服务接入与自动故障切换 —— 验收证据

> 依据：`docs/superpowers/specs/2026-09-12-dual-ai-provider-failover-design.md`
> 实施计划：`docs/superpowers/plans/2026-09-12-dual-ai-provider-failover.md`
> 执行提示词：`docs/superpowers/prompts/2026-09-13-trae-dual-ai-provider-execution-prompt.md`

## 1. 快照信息

- 文件快照日期：2026-09-13
- 未初始化 Git：`Test-Path .git` = `False`（无 commit，以文件快照为准）
- 内置目录版本：`provider_catalog.builtin.json` → `version = 1`，`default_provider_id = deepseek`

## 2. Preflight 测试基线（改造前）

### 针对性基线

```
Set-Location backend; py -3.12 -m pytest tests/providers/test_deepseek.py tests/providers/test_factory.py tests/providers/test_connectivity.py tests/search/test_rewrite.py tests/backfill/test_backfill.py tests/resumes/test_validity.py tests/test_runtime.py -q
```

结果：**1 failed, 44 passed**

失败 node ID：

- `tests/search/test_rewrite.py::test_rewrite_cache_misses_on_lexicon_version_change`（硬编码 `LEXICON_VERSION="2"`，与生产值相同导致缓存未失效；已在 Task 6 改为 `cache_identity` 并修正，未弱化缓存失效断言）。

### 后端全量基线

`Set-Location backend; py -3.12 -m pytest -q` → 2 个预先存在的 collection error（非本次引入）：

- `tests/search/test_sync.py` → `ModuleNotFoundError: No module named 'kerui_recruit.soft_delete.service'`
- `tests/soft_delete/test_soft_delete.py` → 同上

这两个测试文件引用已被删除的「软删除/回收站」模块，属回收站下线遗留，不在本任务范围内清理。

## 2.1 最终复验结果（2026-09-13，以真实源码与测试为准）

以下为本轮最终复验的新鲜命令与输出，覆盖目标的八条验收命令：

### 后端专项（目标的 `py -3.12 -m pytest tests/providers/ai ...` 全列表）

```
Set-Location backend
py -3.12 -m pytest tests/providers/ai tests/api/test_ai_settings.py tests/test_runtime.py tests/test_sidecar.py tests/providers/test_ocr.py tests/providers/test_factory.py tests/search/test_rewrite.py tests/backfill/test_backfill.py tests/resumes/test_pipeline_ocr.py tests/resumes/test_validity.py -q
```

结果：**140 passed, 1 warning**（含角色路由 7 条 + A–I 端到端 + 角色路由集成测试 + Case H 计数 + `test_g_deadline_exhaustion_falls_back_to_original_query`）。

### 后端全量（排除两个既有 soft_delete 收集错误文件）

```
Set-Location backend
py -3.12 -m pytest -q --ignore=tests/soft_delete --ignore=tests/search/test_sync.py
```

结果：**4 failed, 763 passed, 2 skipped, 1 deselected**（约 2:49）。4 个失败全部为**预先存在**（非本任务引入，均不在 AI 改动文件范围内）：

- 2 个 soft_delete 遗留：`tests/api/test_jd_flow.py::test_candidate_soft_delete_is_rejected`（`assert 404 == 400`）、`tests/cases/test_case_service.py::test_job_delete_and_restore_pause_only_unfinished_linked_reminders`（`ModuleNotFoundError: kerui_recruit.soft_delete.service`）。
- 2 个时间敏感：`tests/api/test_full_acceptance.py::test_desensitized_sample_end_to_end`（TimeoutError）、`tests/search/test_consistency.py::test_timed_out_candidate_scan_does_not_start_more_native_queries`（`assert 0 == 1`）。

2 skipped：`tests/api/test_real_pdf_review.py`（需 `KERUI_REVIEW_PDF` 私有 PDF）。

两个既有 soft_delete **收集错误**（原样记录，不算本任务新增失败，也未用 ignore/skip 隐藏新失败）：

```
py -3.12 -m pytest tests/soft_delete/test_soft_delete.py tests/search/test_sync.py -q
```

→ `ERROR tests/soft_delete/test_soft_delete.py`、`ERROR tests/search/test_sync.py`（均 `ModuleNotFoundError: No module named 'kerui_recruit.soft_delete.service'`），`2 errors in 9.77s`。

### 前端

```
Set-Location desktop
npm test -- --run → 9 passed (9 files), 89 passed
npm run build → exit 0（tsc -b && vite build）
```

### E2E（Playwright，注入 httpx MockTransport，不访问真实供应商）

```
Set-Location desktop
$env:KERUI_E2E_BACKEND_PORT="43128"; $env:KERUI_E2E_FRONTEND_PORT="1421"
npx playwright test tests/ai-settings.spec.ts → 1 passed (10.7s)
```

（默认端口 43127/1420 被遗留开发进程占用，故以 43128/1421 运行。）

### 密钥/越界扫描

```
rg -n "api_key.*(print|log)|logger.*api_key|reasoning_content.*(print|log)|sk-[A-Za-z0-9]{12,}" backend/src desktop/src
```

→ 无匹配（exit 1）。

```
rg -n "chat/completions" backend/src/kerui_recruit --glob '!providers/ai/openai_chat.py'
```

→ 仅 `providers/ai/openai_chat.py:87`（唯一活动生成式 POST）、`providers/openai_compatible.py:26`（已废弃兼容包装的 docstring，委托给 `OpenAIChatAdapter`，不再自行 POST）。

## 3. 验收期测试结果

### 后端（全量，排除预先存在的 collection error）

```
Set-Location backend; py -3.12 -m pytest -q --ignore=tests/soft_delete --ignore=tests/search/test_sync.py
```

最终结果：**6 failed, 725 passed, 2 skipped, 1 deselected**（约 3:44）。多次运行中失败数在 6–8 之间波动，全部为**预先存在**（非本任务引入，均不在 AI 改动文件范围内）：

- 2 个 soft_delete 遗留（`test_jd_flow.py::test_candidate_soft_delete_is_rejected`、`test_case_service.py::test_job_delete_and_restore_pause_only_unfinished_linked_reminders`，引用已删除的 `kerui_recruit.soft_delete.service`）。
- 1 个端到端超时（`test_full_acceptance.py::test_desensitized_sample_end_to_end`，TimeoutError）。
- 3–5 个时间敏感搜索测试（`test_consistency.py` / `test_service.py` 中以 `_timeout`/`_deadline`/`slow_reranker`/`timed_out` 结尾者），受本机负载影响在运行间通过/失败波动。

2 skipped：`tests/api/test_real_pdf_review.py`（需 `KERUI_REVIEW_PDF` 私有 PDF 才运行）。

**AI 专项全绿**：

```
py -3.12 -m pytest tests/providers/ai/ tests/api/test_ai_settings.py tests/test_runtime.py tests/test_sidecar.py tests/providers/ai/test_catalog.py -q → 63 passed
py -3.12 -m pytest tests/api/test_ai_settings.py -q → 7 passed（含新增 probe 回归测试）
```

### 前端

```
Set-Location desktop; npx tsc -b --noEmit → exit 0
Set-Location desktop; npm run build → exit 0（tsc -b && vite build）
Set-Location desktop; npx vitest run → 88 passed（9 test files）
```

### E2E

`desktop/tests/ai-settings.spec.ts`（DeepSeek 主 + 通义千问备用 非技术 happy path）：

```
Set-Location desktop; npx playwright test tests/ai-settings.spec.ts
```

结果：**1 passed（11.4s）**。流程为：进入设置 → 添加主服务 DeepSeek → 填假 Key（`e2e-deepseek-key`）→ 检测并继续（探测端点上游 auth 失败仍返回 200 脱敏报告）→ 保存为主服务 → 添加备用服务通义千问 → 填假 Key → 检测并继续 → 保存为备用服务 → 显示「双服务保护已开启」。

> E2E 运行环境说明：默认后端端口 43127 与前端端口 1420 被遗留开发进程占用，本次以 `KERUI_E2E_BACKEND_PORT=43128`、`KERUI_E2E_FRONTEND_PORT=1421` 运行；探测使用假 Key，不消耗真实额度，也不发送真实候选人数据。

**E2E 过程中发现并修复的回归**：`POST /api/ai/probe` 此前对 `CapabilityProbe`（`@dataclass(frozen=True, slots=True)`）调用 `.__dict__` 导致 500；已改为 `dataclasses.asdict(...)`，并新增 `tests/api/test_ai_settings.py::test_probe_uses_unsaved_key_without_echoing_it` 回归测试（`py -3.12 -m pytest tests/api/test_ai_settings.py -q → 7 passed`）。

## 4. 故障切换用例证据（Cases A–I）

故障切换核心逻辑由 `backend/tests/providers/ai/test_router.py` 与 `test_manager.py` 覆盖（`py -3.12 -m pytest tests/providers/ai/ -q` 全绿）：

| Case | 结论 | 证据测试 |
| --- | --- | --- |
| A：DeepSeek 429 → Qwen 有效 JSON，业务成功并汇报切换，恰好两次上游请求 | 通过 | `test_429_switches_once_to_secondary`（`primary.calls == 1 and secondary.calls == 1`）、`test_successful_fallback_records_sanitized_summary` |
| B：输入/策略拒绝不切换 | 通过 | `test_input_error_never_sends_data_to_secondary`（`secondary.calls == 0`） |
| C：双 503 → 「主服务和备用服务当前均不可用」 | 通过 | `test_both_fail_raises_all_providers_failed`（`E_AI_ALL_PROVIDERS_FAILED`） |
| D：冷却后主服务恢复 | 通过 | `test_primary_is_retried_after_cooldown_and_restored` |
| E：Kimi Code 后台/批量不调用 | 通过 | `test_kimi_code_is_filtered_from_background_and_batch`（`kimi.calls == 0`） |
| F：取消在途主请求 → `CancelledError` 传播，0 次备用 | 通过 | `test_cancelled_primary_never_calls_secondary` |
| G：主服务耗尽共享 deadline → 0 次备用，且搜索回退原查询 | 通过 | `test_exhausted_shared_deadline_never_starts_secondary`（`E_AI_DEADLINE`）、`test_g_deadline_exhaustion_falls_back_to_original_query`（真实 `SemanticQueryRewriter` + Manager：deadline 耗尽后 `result.status == "unavailable"`、`result.query` 回退原查询、主/备均 0 次远程调用） |
| I：模型路由变化 → `cache_identity` 变化 | 通过 | `test_manager.py::test_saved_config_is_visible_to_next_request_without_runtime_rebuild`、`test_cache_identity_is_secret_free` |

Case H（Pydantic 有效但业务不完整的简历 → 沿用 `E_PARSE_INCOMPLETE`/视觉重解析，而非泛化自动切换）：由带调用计数的真实流水线测试覆盖 `test_manager.py::test_incomplete_resume_does_not_trigger_backup_text_call` —— 主服务 `deepseek-v4-flash` 调用 1 次、备用 `qwen3.8-flash` 调用 0 次，`check_parsed_resume` 返回 `E_PARSE_INCOMPLETE`，未改动 `E_PARSE_INCOMPLETE` 与视觉重解析流程。

## 5. 打包资源与离线冒烟

- `kerui-recruit-sidecar.spec` 已新增 `provider_catalog.builtin.json` 到 `datas`（目标目录 `kerui_recruit/providers/ai`），未移除 `jieba` data/submodule 收集。
- 打包命令：`py -3.12 -m PyInstaller --noconfirm kerui-recruit-sidecar.spec`
- 可执行文件路径：`dist/kerui-recruit-sidecar.exe`
- SHA-256：`20C8C95DEBF398AABCC6BFB968C87D008E2B23E0947A77E3E25BF7A65F96B9C0`
- 离线冒烟：以临时数据根目录、清空 `KERUI_AI_CATALOG_URL`/`KERUI_AI_CATALOG_PUBLIC_KEY_B64` 启动打包 sidecar，`GET /api/ai/catalog` 返回 `default=deepseek`、`version=1`、7 个入口 `deepseek,kimi_open,kimi_code,qwen,zhipu,siliconflow,custom_openai`（无网络调用，资源自 bundled datas 解析）。
- `test_sidecar.py::test_builtin_catalog_resolves_as_packaged_resource` 验证内置目录经 `importlib.resources` 可解析且含七个入口。

## 6. 密钥扫描

```
rg -n "api_key.*(print|log)|logger.*api_key|reasoning_content.*(print|log)|sk-[A-Za-z0-9]{12,}" backend/src desktop/src
```

结果：**无匹配**（无密钥/推理内容日志，无硬编码 `sk-` 密钥）。

```
rg -n "chat/completions" backend/src/kerui_recruit --glob '!providers/ai/openai_chat.py'
```

结果：仅 `providers/openai_compatible.py:82`（已废弃、无任何导入的旧类）与 `providers/ai/openai_chat.py`（唯一活动生成式适配器，其 docstring 亦提及）。所有活动生成式请求集中收敛于 `providers/ai/openai_chat.py`。

## 7. 受保护项未变

`rg` 校验以下保留项未被本次改造改动：

- `INDEX_SCHEMA_VERSION = "8"`、`INDEX_CHUNK_VERSION = "5"`
- `query_plan`、`direction`、`REPARSE_FAILED`、`E_PARSE_INCOMPLETE`
- Embedding/Rerank 仍走 SiliconFlow/本地，检索阈值与现有搜索/匹配行为不变

## 8. 官方模型名变化

内置目录 `version = 1` 为初始签名基线，本任务未观察到需要回填的官方模型名变化（远程刷新在未配置 `KERUI_AI_CATALOG_URL`/公钥时返回 `disabled`）。
