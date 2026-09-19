# BD 助手 P4：Agent 式深度网络检索

设计日期：2026-08-30
目标：把 BD 助手从"单次搜索 + 正则提取"升级为"Agent 式多轮深度检索"，具备查询规划、多子查询并发、页面正文抓取、证据重排、带引用综合生成、多轮对话与报告导出。

## 1. 现状与差距

现有实现（`bd_search/service.py` + `providers/websearch.py` + `providers/leads.py`）是一条同步流水线：

```
_enhance_query → 搜一次(Tavily/SerpApi) → 正则/DeepSeek 提公司名 → 加密落库 bd_lead
```

缺口：无查询规划、无正文深度阅读、无证据定位/重排、无带引用综合、无多轮上下文、无报告导出。

## 2. 架构

新增 `bd_agent` 异步模块，复用现有抽象，保留现有 `bd_search` 作为降级路径。

```
用户输入（自然语言 / 候选人档案 / JD）
  → QueryPlanner（DeepSeek JSON 生成子查询）
  → 并发执行：WebSearchProvider(asyncio.to_thread 包装现有同步实现)
  → WebFetcher 抓页面正文（Tavily raw_content 优先，Jina Reader 兜底）
  → EvidenceExtractor 正文切分 + bge-reranker 重排取 top 片段
  → SynthesisGenerator（DeepSeek 带引用生成结构化线索）
  → 落库（session + evidence + lead 扩展）
  → 多轮追问 / 报告导出
```

### 复用（不重写）
- `WebSearchProvider`：Tavily/SerpApi/Null，用 `asyncio.to_thread` 包装
- `OpenAICompatibleClient.complete_json`：QueryPlanner / Synthesis
- `SiliconFlowRerankerProvider.rerank`：证据排序
- `EncryptionService`：PII 加密
- `AppServices` / `runtime.build_runtime`：装配

## 3. 新增组件（`backend/src/kerui_recruit/bd_agent/`）

| 文件 | 职责 |
| --- | --- |
| `fetcher.py` | `WebFetcher` 协议 + `JinaReaderFetcher`（`GET https://r.jina.ai/{url}` 转 markdown）；失败返回 None |
| `planner.py` | `QueryPlanner`：LLM 生成 `SearchPlan{sub_queries: list[str]}`；LLM 不可用回退单 query |
| `evidence.py` | `EvidenceExtractor`：正文切段 → reranker 排序 → top 证据片段（无 reranker 取前 N） |
| `synthesis.py` | `SynthesisGenerator`：LLM 读证据生成带 citation 的结构化线索 |
| `agent.py` | `BdAgent`：编排循环，`max_rounds`/`max_queries` 预算，降级到 `BdSearchService` |

## 4. 数据模型（schema v2 → v3）

新增表：
- `bd_search_session`：`id, query, kind, status, created_at, updated_at`
- `bd_evidence`：`id, lead_id(FK), claim, quote, source_url, relevance_score, created_at`

扩展 `bd_lead`：
- `confidence: float | None`
- `is_hiring: bool | None`
- `session_id: str | None`（FK bd_search_session.id）
- `synthesized_json: JSON | None`

迁移：`Upgrade(2, 3, _upgrade_v2_to_v3)`，`SCHEMA_VERSION = 3`。新表也由 `create_all` 建；`bd_lead` 新列用 `ALTER TABLE`。

## 5. API（`api/bd_agent.py`）

- `POST /api/bd/agent/query`：`{query, kind}` → `{session_id, leads[]}`
- `POST /api/bd/agent/session/{id}/follow-up`：`{query}` → 追加轮次，返回新线索
- `GET /api/bd/agent/session/{id}/export`：报告导出（Markdown）

保留 `/api/bd/*` 作为无 LLM 时的降级路径。

## 6. 降级策略

| 条件 | 行为 |
| --- | --- |
| DeepSeek 不可用 | QueryPlanner/Synthesis 跳过，回退 `BdSearchService`（正则） |
| Tavily/SerpApi 均无 Key | 返回空 + 提示手动浏览器 |
| reranker 不可用 | 跳过重排，取前 N 片段 |
| 页面抓取失败 | 用 snippet 兜底 |

## 7. 前端（`desktop/src`）

- BD 助手 tab 改为对话式输入 + 线索卡片（公司/岗位/是否在招/置信度/证据引用可点击）
- 多轮追问输入框
- 报告导出按钮
- `App.tsx` 新增类型 + `client.ts` 新增方法

## 8. 验收标准

- 输入自然语言问题，返回结构化线索且每条结论带 source_url 引用
- 同一 session 可多轮追问，追问不重复计费已有缓存
- 无 LLM/搜索 Key 时优雅降级，不阻断其它模块
- 后端 `pytest` 全绿；前端 `npm test` 全绿
