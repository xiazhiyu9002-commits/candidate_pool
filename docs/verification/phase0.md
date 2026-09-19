# Phase 0 验收证据

**日期：** 2026-08-29  
**依据：** `docs/superpowers/plans/2026-08-29-phase0-search-foundation.md`

## 完成范围

Phase 0 全部 11 个任务均已实现，覆盖：SQLite 事实库、内容寻址原件库、持久化任务租约、Provider 契约、简历解析管线、LanceDB 混合检索、本地安全 API、Tauri/React 桌面壳与 sidecar 生命周期、以及 12 万候选的检索性能门槛。

## 自动化证据

| 层 | 命令 | 结果 |
| --- | --- | --- |
| 后端单元/集成 | `pytest backend/tests -m "not performance"` | 51 passed |
| 后端性能门槛 | `pytest backend/tests/performance -m performance` | 通过（12,000 候选、200 查询、召回 98%+，p95 ≤ 1.5s） |
| 桌面 Rust 单元 | `cargo test`（`desktop/src-tauri`） | 2 passed |
| 桌面 React 单元 | `npm test`（`desktop`） | 5 passed |
| 桌面 E2E | `npm run test:e2e -- search.spec.ts`（需打包 sidecar） | 见下方说明 |

## 性能门槛

`BenchmarkApp` 使用确定性本地 Provider（无实时 API Key），在 12,000 候选规模验证：

- `error_rate == 0`
- `p95_ms <= 1500`
- `p99_ms <= 2500`
- `recall_at_300 >= 0.98`

生产发布门槛按 `python -m kerui_recruit.bench.search_benchmark --candidates 120000 --queries 800 --concurrency 10 --qps 5` 运行，报告写入 `docs/verification/phase0.md` 同级的 JSON 产物。

## 桌面壳交付说明

Rust 壳层实现了随机回环端口选择、256 位启动令牌生成、sidecar 启动与 `/health/ready` 等待，以及应用退出时终止 sidecar。

sidecar 打包通过 PyInstaller spec（`backend/packaging/kerui_recruit.spec`）与 `backend/packaging/build_sidecar.ps1` 完成，产物为 `dist/kerui-recruit-sidecar.exe`。已在本机（Windows 11 x64）完成验证：

- 打包产物启动后 `/health/ready` 返回 `200 {"status":"ready"}`；
- 通过打包产物走真实 HTTP 闭环：`POST /api/resumes/import` 返回 `202` 及 candidate/revision/task id，随后 `POST /api/search/candidates` 命中该候选人（`bm25` + `vector` 双通道）。

Rust 壳层通过优先级 `KERUI_SIDECAR_BIN` 环境变量 → 与桌面可执行文件同目录的 `kerui-recruit-sidecar[.exe]` 定位打包产物。macOS arm64 构建需在对应平台执行同一 spec，不在 Windows 交叉打包。

桌面壳层已在本机（Windows 11 x64）完成完整冒烟：

- `cargo build` 产出 `kerui-recruit-desktop.exe`；
- 启动后壳层通过 `RuntimeConfig::allocate` 随机回环端口 + 256 位令牌自动拉起 sidecar，`wait_until_ready` 确认 `/health/ready` 后进入运行态（壳层存活 8s+）；
- 优雅关闭（`CloseMainWindow`）触发 `RunEvent::Exit`，经 `taskkill /T /F` 完整终止 sidecar 进程树，验证无孤儿进程。

> 注：PyInstaller one-file 会 fork 出 bootloader + 真实解释器两个进程，直接 `child.kill()` 只终止 bootloader、留下孤儿服务进程。已在 `terminate_sidecar` 中通过 Windows `taskkill /T` 终止整棵进程树修复。

Playwright E2E（`npm run test:e2e`）需打包后的 Tauri 壳层与 sidecar 同机运行；本机已完成 sidecar HTTP 闭环与壳层启动/退出冒烟，未在无头环境跑浏览器端 E2E。

## 真实模型 Provider 接入

按 spec 第 9 章 Provider 适配层接入真实 API，通过环境变量注入、不落明文仓库：

- **LLM（DeepSeek）**：`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`（默认 `deepseek-v4-flash`，`base_url=https://api.deepseek.com`）。
- **Embedding + Reranker（硅基流动）**：`SILICONFLOW_API_KEY` / `SILICONFLOW_BASE_URL` / `SILICONFLOW_EMBEDDING_MODEL`（`BAAI/bge-m3`）/ `SILICONFLOW_RERANKER_MODEL`（`BAAI/bge-reranker-v2-m3`）。

机制：`providers/factory.py` 按 `Settings` 决定本地（离线，64 维哈希向量）或真实（1024 维 bge-m3）Provider；`sidecar.py` 从环境变量读取密钥。搜索投影维度随 embedding 选择自动切换。

已验证（真实 API key、干净数据目录、单进程闭环）：

- DeepSeek `deepseek-v4-flash` 结构化解析简历 → `revision READY`；
- 硅基流动 `BAAI/bge-m3` 生成 1024 维向量、`BAAI/bge-reranker-v2-m3` 重排；
- 导入后 `POST /api/search/candidates` 命中新候选人，`task_status=SUCCESS`。

> 切换真实/本地 embedding 会改变向量维度（64 ↔ 1024），属检索版本变化；旧索引不兼容，需用新的数据目录或重建索引。