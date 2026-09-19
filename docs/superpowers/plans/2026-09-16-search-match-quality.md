# 搜索与人岗匹配质量改造 Implementation Plan

> **For agentic workers:** 按任务逐项执行并使用复核关口；可用 `superpowers:executing-plans`，不要一次性改完整条链路。每个任务用 `- [ ]` 跟踪，先验证失败用例，再做最小改动，最后记录证据。

**Goal:** 修复人才库搜索的误筛/漏检，改善向量与混合召回，统一双向匹配资格和排序，并提供默认关闭的 AI 深度复核。

**Architecture:** 搜索、结构化资格、匹配排序和可选 AI 复核分层。先冻结样本与基线，再修确定性缺陷；向量文档与排序改动通过隔离索引消融决定；两个匹配入口复用候选人—JD 配对评估器。AI 复核是异步后处理，不阻塞基础结果。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLAlchemy/SQLite、LanceDB、pytest；React 19、TypeScript、Vite/Vitest、Tauri 2。

**Spec:** `docs/superpowers/specs/2026-09-16-search-match-quality-design.md`

## Global Constraints

- 本计划是实施指令，**当前尚未实施**。现行源码优先于任何旧文档；`docs/superpowers/plans/2026-09-15-strict-dual-match.md` 及早期 `.trae` 记录与本文冲突时以本设计及当前源码为准。
- 用户已定：数据工程按主要职责区分；AI 深度复核必须是前端可选、默认关闭；匹配按钮不展示关键词/向量/混合三选项。
- 搜索框保留高级检索模式和精确筛选；用户可见的精确条件不得被隐式 AI 改写。`ai_category` 与 `direction` 独立。
- 真实简历、手机号、密钥与 AI 提示词正文不得进入测试夹具、Trae 对话、日志或评测报告；只用脱敏/合成数据。应用运行时的模型继续使用现有 provider 管理器，不能把 Trae 的编码模型当成应用模型。
- 当前工作区 `Test-Path .git` 为 `False`。Trae 启动时再查；若仍无 Git，不初始化仓库、不伪造提交，用 `.trae/search-match-quality-progress.md` 记录每个任务的文件、命令、结果与回滚点；若有 Git，每个通过的任务独立提交。
- Windows/Tauri、既有 outbox 同步和原有 API 消费方都要保持可运行。索引结构/文本改变时必须升版本并 staging 重建，不能在活跃 `.dev-data` 上直接做实验。

---

## 给 Trae Agent / DeepSeek V4 Pro 的工作方式

Trae 官方描述 IDE/SOLO 可执行编码任务，Rules 和自定义 Agent 提示词有优先级；本项目建议只将以下短规则放进 Trae 项目规则，完整细节保留在本文件，避免把大量过时文档塞进模型上下文。DeepSeek 官方列出 JSON 输出和工具调用能力，但对 `deepseek-v4-pro` 的后续路由说明在不同官方页面存在差异；Trae 选中该模型时先做最小文件读取、终端执行与编辑探测，记录实际模型标识和可用性。不要以宣传的长上下文取代分任务验证。[Trae 说明](https://www.trae.ai/blog/trae_work_0609)、[Trae Rules](https://www.trae.ai/blog/trae_tutorial_0825?v=1)、[DeepSeek 模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek 更新日志](https://api-docs.deepseek.com/updates/)。

可复制到 Trae 新会话的**总控提示词**：

```text
在当前 candidate_pool 工作区实现 docs/superpowers/plans/2026-09-16-search-match-quality.md，
并先读对应 spec。旧文档仅作历史背景，实施前逐项核对当前源码与测试。
每次只执行计划中的一个 Task；先报告将改的文件、现有接口和失败测试，
再运行 RED→最小实现→GREEN→受影响回归。失败或发现接口已变化时停在本 Task，
报告证据并调整该 Task，不得继续叠加后续改动。
不要读取/输出真实简历全文或密钥，不改活跃 .dev-data；实验用快照和 staging。
每个 Task 结束报告：变更文件、关键行为、测试命令与结果、样本指标、剩余风险。
阶段门未通过不执行下一阶段。不要自行把 AI 深度复核设为默认开启。
```

推荐 Trae 每个新会话只带本计划的**当前 Task**、所列源码和上一 Task 的结果摘要；每做完一个任务重新读取相关文件。简单确定性修正用较低推理预算，方向边界、融合排序及迁移用较高预算；最终正确性由测试与人工标注决定。不要要求模型“一次性优化整库”。

## 文件责任图与接口边界

| 责任 | 主要现有文件 | 拟新增/调整接口 |
| --- | --- | --- |
| 查询意图、字段过滤 | `backend/src/kerui_recruit/search/query.py`, `api/search.py`, `search/contracts.py` | `ParsedQuery` 加可解释解析信息；API 分页与查询计划 |
| 向量/词法文档及索引 | `search/documents.py`, `search/sync.py`, `search/lancedb_index.py`, `search/rebuild_maintenance.py` | 分开的 FTS 范围、候选人级融合、版本化索引 |
| 检索编排与重排 | `search/service.py`, `search/lexicon.py` | 可配置召回池、查询概念覆盖、证据段重排 |
| 方向分类 | `providers/generation_tasks.py`, `resumes/structured.py`, `jd/structured.py` | 新 `direction/policy.py` 定义同一组枚举和边界规则；新 `direction/classifier.py` 负责结构化再判定 |
| 匹配资格与评分 | `match/service.py`, `match/jd_index.py`, `api/match.py` | 新 `match/policy.py` 产出 `PairDecision`；双向入口复用 |
| 可选 AI 复核 | `providers/ai/contracts.py`, `runtime.py`, `db/models.py`, `db/migrate.py`, `api/match.py` | 新 `match/review.py`、复核任务/结果持久化及 start/status API |
| 前端 | `desktop/src/App.tsx`, `pages/TalentPoolPage.tsx`, `pages/JdManagementPage.tsx`, `api/client.ts` | 查询条件标签、匹配单入口、AI 复核开关及进度/证据 |

复用现有 `SearchChunk`、`SearchHit`、`CandidateFilters` 和 `SearchPage`。新增字段要先扩展后端响应与 TypeScript 类型，再移除旧 UI 参数；迁移期间保留旧 API 默认值或兼容适配，避免现有调用者突然失败。

## 阶段 0：基线与验收样本

### Task 1：冻结实验输入，建立可复算的质量基线

**Files:** 新增 `backend/src/kerui_recruit/evaluation/retrieval.py`、`backend/tests/evaluation/test_retrieval_metrics.py`、`backend/tests/fixtures/search_quality_v2.json`、`backend/tests/fixtures/match_quality_v2.json`；记录 `.trae/search-match-quality-progress.md`。读取 `backend/tests/fixtures/search_golden.json`、`backend/src/kerui_recruit/bench/search_acceptance.py` 和当前索引元数据。

**Interfaces:** `evaluate_search(cases, ranked_ids) -> dict` 输出 `recall_50`、`recall_100`、`precision_10`、`ndcg_10`；`evaluate_match(pairs, decisions) -> dict` 输出 `precision_5`、`precision_10`、`hard_violation_count`。标注文件只存匿名 ID、查询和等级，不存简历原文。

- [ ] 先读现有测试/评测入口和当前 `.dev-data` 路径，记录数据库/索引文件哈希、当前候选人与 JD 数、embedding 模型、chunk/schema 版本；对活跃库只读，实验拷贝放工作区外的独立目录。
- [ ] 写指标单测：排序 `[A,B,C]`，相关集合 `{A,C}` 时 `recall@2=0.5`、`precision@2=0.5`；重复候选人 ID 只计一次；无相关标签不并入平均值。先运行 `python -m pytest tests/evaluation/test_retrieval_metrics.py -q`，应因模块不存在失败。
- [ ] 实现指标函数并再次运行该命令至通过；从业务查询抽取至少 40 个搜索意图与至少 80 个匹配配对供**人工确认**，包括学校/城市冲突、技能缩写、AND/OR、方向边界和核心职责。没有人工标签时可以完成代码基线，但不得宣称质量提升。
- [ ] 旧版在同一快照跑搜索与匹配基线，保存各 query 的匿名 Top 100、阶段时延、embedding/rerank 次数及失败案例；分开调参集和锁定验收集。`local-hash-v1` 只能测流程，不作为语义质量证据。
- [ ] 运行 `python -m pytest tests/evaluation tests/search tests/match -q`，记录新增/既有失败 node ID；阶段门是评测能复算、标注集有来源、活跃库无写入。

## 阶段 1：搜索正确性

### Task 2：查询解析不产生错误硬筛

**Files:** 修改 `backend/src/kerui_recruit/search/query.py`, `backend/src/kerui_recruit/api/search.py`；测试 `backend/tests/search/test_query.py`, `backend/tests/api/test_search_consistency.py`；前端条件标签留 Task 4。

**Interfaces:** 保持 `parse_query(text, school_alias_groups=...) -> ParsedQuery` 兼容；`ParsedQuery` 增加解析来源和置信信息，`QueryPlanResponse` 显示最终生效的硬条件和保留的关键词。

- [ ] 写失败样例：`北京大学 后端` 不产生 `location=北京`；`上海交通大学 Java` 不产生 `location=上海`；`现居北京 后端` 产生北京地点；`排除外包 Java` 的排除对象不能被当技能名静默忽略，应以明确排除概念或未支持提示回显。运行对应 `test_query.py` 和 API 测试看到预期失败。
- [ ] 对城市识别使用显式地点语法、词边界和已识别学校/公司实体保护；不确定片段保留词法查询。为现有 `ParsedQuery` 增加来源列表，不改变用户手填 `filters` 的优先级。
- [ ] 运行 `python -m pytest tests/search/test_query.py tests/api/test_search_consistency.py -q`；人工检查回显条件能解释每个硬筛。

### Task 3：正文开关、limit 与筛选分页语义一致

**Files:** 修改 `backend/src/kerui_recruit/search/lancedb_index.py`, `backend/src/kerui_recruit/search/service.py`, `backend/src/kerui_recruit/api/search.py`, `backend/src/kerui_recruit/search/contracts.py`；测试 `backend/tests/search/test_service.py`, `backend/tests/search/test_filters.py`, `backend/tests/api/test_filter_only_request.py`, `backend/tests/api/test_pagination.py`。

**Interfaces:** `search_fts(query, filters, limit, search_body=False)` 在 false 时只召回父文档的简历概况字段；true 时额外召回经历/项目正文。`CandidateSearchRequest.limit` 控制当前页大小；响应加稳定分页 token 或 `offset`、`has_more`，并标明总量是否准确。

- [ ] 写失败样例：项目正文独有词在 `search_body=false` 时不命中、true 时命中；查询请求 `limit=7` 最多返回 7 个唯一候选人；筛选-only 连续两页无重复/遗漏，编辑后旧游标不能混入新 revision。先运行聚焦测试确认失败。
- [ ] 将 FTS 父/子范围明确下沉到索引查询，避免 `keyword_index_text` 子 chunk 绕开正文开关；修复 `api/search.py` 的固定 300 和搜索服务的 30 条隐式截断对普通搜索请求的影响。稳定排序由分数、候选人 ID 和 revision 共同确定；分页从候选人级结果产生。
- [ ] 运行 `python -m pytest tests/search/test_service.py tests/search/test_filters.py tests/api/test_filter_only_request.py tests/api/test_pagination.py -q`；确认 4.5 秒既有搜索预算没有被同步 AI 调用吞掉。

### Task 4：搜索框明确展示查询计划与命中证据

**Files:** 修改 `desktop/src/pages/TalentPoolPage.tsx`, `desktop/src/App.tsx`, `desktop/src/api/client.ts`, `backend/src/kerui_recruit/api/search.py`；测试 `desktop/src/pages/TalentPoolPage.test.tsx`, `backend/tests/api/test_search_consistency.py`。

**Interfaces:** 页面默认“智能搜索”，高级选项保留关键词/向量/混合、AND/OR、AI 改写和精确筛选。响应的 `query_plan` 显示生效条件、词法/语义查询及降级状态；每条结果显示字段/经历/项目命中片段和来源 ID。

- [ ] 写前端失败用例：搜索“北京大学 后端”不出现现居北京标签；手填北京过滤出现可取消条件标签；关闭正文开关时 UI 不声称正文被检索；匹配按钮状态不受搜索 mode 改变。
- [ ] 扩展 API 证据结构，保证片段来自当前 revision 且长度受限；前端显示并允许移除仅由查询框推断的条件，再发起检索。姓名、手机号等敏感值在日志与错误状态不回显。
- [ ] 运行 `npm test -- --run src/pages/TalentPoolPage.test.tsx` 与 `npm run build`（工作目录 `desktop`），再跑搜索 API 测试。阶段门：上述误筛用例、正文语义与分页全通过。

## 阶段 2：召回、排序与向量文本

### Task 5：按候选人融合父子证据与重排

**Files:** 修改 `backend/src/kerui_recruit/search/lancedb_index.py`, `backend/src/kerui_recruit/search/service.py`, `backend/src/kerui_recruit/search/contracts.py`, `backend/src/kerui_recruit/search/lexicon.py`；测试 `backend/tests/search/test_hybrid.py`, `backend/tests/search/test_relevance_golden.py`, `backend/tests/search/test_lexicon.py`。

**Interfaces:** 融合结果仍是一个 `SearchHit`/候选人，新增至多 3 条有来源的 `evidence_chunks`；RRF 在各通道按候选人聚合后计算；重排读取画像 + 至多 2 条命中经历/项目片段。保留各通道原始分和重排分，不把 RRF 当概率。

- [ ] 写失败样例：同一候选人父 chunk 在 FTS 第 2、项目子 chunk 在向量第 2 时，最终候选人 `matched_channels` 同时包含两通道；一个人 20 个子 chunk 不挤走另一个独立候选人；候选人顺序同分时稳定；Agent/智能体同义词仅对技术概念扩展，不误伤普通中文。
- [ ] 先运行 `python -m pytest tests/search/test_hybrid.py tests/search/test_relevance_golden.py tests/search/test_lexicon.py -q`，确认新用例失败，再改索引融合和重排取证。`KEYWORD_MIN_HIT_TERMS=1`、固定向量阈值及全局 top30 只在标注集试验后修改；召回池与最终显示数分别配置。
- [ ] 同一快照比较旧/新 `Recall@50/100`、`nDCG@10`、p95、每次重排文档长度；命中片段可回指原 revision 才过门。

### Task 6：候选人和 JD 向量文档消融，按结果选择变体

**Files:** 修改候选变体 `backend/src/kerui_recruit/search/documents.py`, `backend/src/kerui_recruit/search/sync.py`；若胜出再改 `backend/src/kerui_recruit/search/lancedb_index.py` 与 `backend/src/kerui_recruit/match/jd_index.py` 的版本；测试 `backend/tests/search/test_documents.py`, `backend/tests/search/test_sync.py`, `backend/tests/search/test_rebuild_maintenance.py`, `backend/tests/match/test_reverse_index.py`。隔离索引用 `backend/src/kerui_recruit/search/rebuild_maintenance.py`，不可直接写 `.dev-data`。

**Interfaces:** 候选变体 A 为现状；B 为画像 + 从已有 `skills`/项目 `tech_stack` 去重、限长生成的技术栈概览父向量、独立工作/项目子向量，学校/城市/年限等保留元数据/FTS，不新增生成模型调用；C 在 B 上把项目名称和业务场景合入项目段。JD 变体 A 保留单技能子向量；B 只保留整体画像/职责子段，技能仍在 FTS 与硬规则中。

- [ ] 在合成记录上写失败测试：电话/姓名不进入向量；学历、城市、年限继续可精确过滤；工作和项目独立证据不丢；修改项目名称/业务场景会生成对应新向量；短空段不生成向量。先运行聚焦测试确认失败。
- [ ] 对每个变体在同一只读快照、同一 embedding 模型、同一参数和相同 Top K 跑搜索/匹配；记录文档数、总 token、索引大小、`Recall@50/100`、`nDCG@10` 和无结果用例。不要借模型或阈值变化冒充文档收益。
- [ ] 若 B/C 在锁定验收集有收益且硬过滤无退化，才将胜出变体写入生产文档构造，并同步提升 `INDEX_CHUNK_VERSION`/必要时 `INDEX_SCHEMA_VERSION`；采用新 staging 路径重建、`validate` 检查、关闭应用后切换、保留旧索引回滚。若无收益，保留现状并记录消融结论。
- [ ] 运行 `python -m pytest tests/search/test_documents.py tests/search/test_sync.py tests/search/test_rebuild_maintenance.py tests/match/test_reverse_index.py -q`；阶段门是同版本索引一致、旧/新向量不混写、增量编辑/删除仍正确。

## 阶段 3：方向判定与回填

### Task 7：审计 AI 预设选择并统一职业方向规则

**Files:** 新增 `backend/src/kerui_recruit/direction/policy.py`, `backend/src/kerui_recruit/direction/classifier.py`；修改 `backend/src/kerui_recruit/providers/generation_tasks.py`, `backend/src/kerui_recruit/resumes/structured.py`, `backend/src/kerui_recruit/jd/structured.py`；测试新增 `backend/tests/direction/test_policy.py`, `backend/tests/direction/test_classifier.py` 和现有 `backend/tests/resumes/test_pipeline.py`, `backend/tests/jd/test_jd_pipeline.py`；审计记录写入 `.trae/search-match-quality-progress.md`。

**Interfaces:** `DirectionDecision(direction, confidence, evidence, taxonomy_version)`；合法方向为 `BACKEND/FRONTEND/ALGORITHM/DATA/OPS/QA/PRODUCT/MANAGEMENT/OTHER/null`，非法值拒收或归待核，不直接入索引。独立规则先依据主职责和项目作证据判断，模型提示词沿用同一分类表及边界例子。

- [ ] 先列出现行 AI 预设选择字段、允许值、Pydantic/数据库校验、下游是否进入硬筛：至少核对 `direction`、JD `ai_category`、`requirements.kind`、简历 `school_level`/`job_level`。统计匿名的空值/非法值/人工改正数；标出本轮必须修的高风险字段，留存基线。
- [ ] 写失败样例：数据管道/平台开发→BACKEND，数仓分析/BI→DATA，模型训练→ALGORITHM，Agent 工具编排与 API 服务→BACKEND；仅出现“AI”一词、职责矛盾或无证据→`null`/待核；`OPERATIONS` 等非枚举不入库；`ai_category=CORE_AI` 不强制 `direction=ALGORITHM`。
- [ ] 运行 `python -m pytest tests/direction -q` 确认失败；实现共用 taxonomy、结构化校验和短提示词，简历/JD 原有解析字段全部保留。方向证据只保留结构化字段路径/短摘录，不能凭职位名唯一决定。
- [ ] 用人工边界集报告混淆矩阵、未知率和强行错分率；`ai_category=CORE_AI` 不改方向判断。运行 `python -m pytest tests/direction tests/resumes/test_pipeline.py tests/jd/test_jd_pipeline.py -q`。

### Task 8：历史方向增量复判与待核流程

**Files:** 新增 `backend/src/kerui_recruit/direction/backfill.py`；修改 `backend/src/kerui_recruit/api/resumes.py`, `backend/src/kerui_recruit/api/jd.py`, `backend/src/kerui_recruit/search/sync.py`，必要时迁移 `backend/src/kerui_recruit/db/models.py`, `backend/src/kerui_recruit/db/migrate.py`；测试新增 `backend/tests/direction/test_backfill.py`、现有 `backend/tests/search/test_edit_sync.py`, `backend/tests/search/test_consistency.py`。

**Interfaces:** 回填仅读已有结构化 resume/JD revision、跳过人工修订和已确定方向；每批记录 `scanned/changed/pending/error`；变更只作用最新 revision 并入队 index sync；候选人方向不确定进入待核名单，JD 方向不确定不能启动自动匹配。

- [ ] 写失败样例：人工修正方向不会被回填覆盖；旧 revision 不更新；`null/OTHER/非法值` 被识别为待核；同一记录重跑不产生重复修订或重复索引脏任务；已确认方向异步同步到检索索引。
- [ ] 运行 `python -m pytest tests/direction/test_backfill.py tests/search/test_edit_sync.py -q` 确认失败，随后实现 dry-run 默认模式、显式执行模式和断点续跑。先在快照 dry-run，给出会变的匿名 ID 数和方向分布，人工抽样核对后再考虑活跃数据回填。
- [ ] 运行 `python -m pytest tests/direction tests/search/test_consistency.py -q`；阶段门是人工修订不丢、待核不静默排除、索引方向与当前 revision 一致。

## 阶段 4：统一匹配主链路

### Task 9：资格判断与 AND/OR 必需技能组

**Files:** 新增 `backend/src/kerui_recruit/match/policy.py`；修改 `backend/src/kerui_recruit/match/service.py`；测试 `backend/tests/match/test_match.py`、新增 `backend/tests/match/test_policy.py`。

**Interfaces:** `evaluate_pair(jd_revision, resume_revision) -> PairDecision`，其中 `eligibility` 为 `eligible/pending/rejected`，`hard_reasons` 与 `evidence` 分开，`soft_features` 不得覆盖硬拒绝。技能表达式把 `Java and Spring` 作为 AND、`Java or Kotlin` 作为 OR；已核实的 MUST 无证据时标为待核/不推荐，绝不靠总分达标。

- [ ] 写失败样例：仅有 Java 不满足 `Java and Spring`；仅有 Spring 不满足该 AND；Java 满足 `Java or Kotlin`；年限/方向通过且必需技能 0 命中时不能因 `_MIN_SCORE=0.4` 被推荐；行业未标硬要求时不能排除；不确定方向进入待核而不是已推荐或静默消失。
- [ ] 运行 `python -m pytest tests/match/test_policy.py tests/match/test_match.py -q` 确认失败；在新 policy 中拆分资格和排序，原 `_OR_CONNECTORS` 不再将 `and/及` 解释为 OR。解析失败的复杂技能要求放待核并展示 JD 原文，不能默默改变语义。
- [ ] 运行 `python -m pytest tests/match -q`；记录配对标注集上的硬条件违规数，必须为 0。

### Task 10：统一双向召回与职责证据评分

**Files:** 修改 `backend/src/kerui_recruit/match/service.py`, `backend/src/kerui_recruit/search/service.py`, `backend/src/kerui_recruit/match/jd_index.py`, `backend/src/kerui_recruit/api/match.py`；测试 `backend/tests/match/test_match.py`, `backend/tests/match/test_reverse_index.py`, `backend/tests/match/test_record_run.py`, `backend/tests/api/test_jd_flow.py`。

**Interfaces:** JD→候选人与候选人→JD 调用 Task 9 的同一 `evaluate_pair`。召回查询使用 3–5 个区分度高的技能/职责概念作词法通道，加职责/画像语义通道；基础池默认目标 50–100 唯一候选人、参数可配，按 `recall@100` 校准。`PairDecision` 评分包含技能覆盖和核心职责证据，不重复给已通过的方向/年限固定分。

- [ ] 写失败样例：同一 JD/candidate pair 从两个入口所得资格、技能组与职责证据一致；职责“支付高并发服务”能引用候选人的项目/工作证据；仅有 Java 而职责完全无关不能凭技能高分排首位；混合召回允许 50–100 个而不被 `HYBRID_RERANK_TOP_K=30` 截断；无向量服务时明确降级而非空白成功。
- [ ] 运行匹配聚焦测试确认失败；实现候选人级宽召回、配对评分及解释。当前有效 JD 约数十个时，反向入口可在可信方向筛后评估所有有效 JD，避免 ANN 无谓漏掉；未来超过实测性能门槛再切 JD 索引召回。
- [ ] 将 `match_run` 的召回配置、资格规则版本、评分版本和 index/model 版本持久化，当前 revision 改动后旧结果标为历史快照。运行 `python -m pytest tests/match tests/api/test_jd_flow.py -q`，比较 `Recall@50/100`、`Precision@5/10` 与 p95。

### Task 11：两个匹配按钮统一调用和展示

**Files:** 修改 `backend/src/kerui_recruit/api/match.py`, `desktop/src/api/client.ts`, `desktop/src/App.tsx`, `desktop/src/pages/JdManagementPage.tsx`, `desktop/src/pages/TalentPoolPage.tsx`；测试 `backend/tests/api/test_jd_flow.py`, `desktop/src/pages/TalentPoolPage.test.tsx` 及新增 `desktop/src/pages/JdManagementPage.test.tsx`。

**Interfaces:** `POST /api/match/jd` 和 `POST /api/match/candidate/{id}` 对前端都返回基本匹配结果及可追踪 `run_id`；兼容旧响应可用并行新 endpoint/适配层过渡。UI 仅展示“匹配”，结果分别标明“推荐 / 待核 / 不匹配”、命中/缺失证据、状态和分页；默认首屏最多约 30 项，完整结果可继续浏览。

- [ ] 写失败用例：修改人才库 `searchMode` 不改变候选人匹配请求；JD 页不再有匹配 mode 三按钮；两个入口结果状态和证据一致；待核项有独立展示；保存历史匹配结果仍可查看。
- [ ] 运行前后端聚焦测试确认失败，随后移除 UI 的 `jdMatchMode` 依赖、让 candidate 匹配不继承 `searchMode`；保持 API 旧调用兼容并标记迁移方式。结果说明来自资格/证据，不显示假精确概率。
- [ ] 运行 `python -m pytest tests/api/test_jd_flow.py tests/match -q`（工作目录 `backend`）、`npm test -- --run` 与 `npm run build`（工作目录 `desktop`）。阶段门：两个按钮都完成基本匹配，无 AI 复核仍可用。

## 阶段 5：前端可选 AI 深度复核

### Task 12：异步复核任务、结构化结论与证据校验

**Files:** 新增 `backend/src/kerui_recruit/match/review.py`；修改 `backend/src/kerui_recruit/providers/ai/contracts.py`, `backend/src/kerui_recruit/runtime.py`, `backend/src/kerui_recruit/api/match.py`, `backend/src/kerui_recruit/db/models.py`, `backend/src/kerui_recruit/db/migrate.py`，复用 `backend/src/kerui_recruit/tasks/repository.py` 与 `backend/src/kerui_recruit/tasks/worker.py` 的现有任务/取消机制；测试新增 `backend/tests/match/test_ai_review.py`, `backend/tests/api/test_match_review.py`, `backend/tests/db/test_match_review_schema.py`。

**Interfaces:** `POST /api/match/run/{run_id}/ai-review` 启动可重试任务，返回 `review_id`；`GET /api/match/run/{run_id}/ai-review` 返回 `not_started/running/completed/partial/failed/cancelled` 和进度。每对结论为 `recommend/pending/reject`、职责覆盖、短理由、引用的 resume/JD 字段与 revision ID；基础 `MatchResult` 不被 AI 覆盖。

- [ ] 写失败用例：未触发复核时 provider 调用数 0；触发后只发送当前 run 的合格配对和必需证据；伪造或过期 evidence ID 被拒绝；候选人/JD 编辑后缓存 miss；断网/超时仅把复核标记 partial/failed，基础结果不变；取消后不继续调用 provider。
- [ ] 运行 `python -m pytest tests/match/test_ai_review.py tests/api/test_match_review.py -q` 确认失败。新增 `TaskKind.MATCH_REVIEW` 与现有 `AiProviderManager.task_client` 路由，Pydantic 严格 JSON schema；按单候选人或小批量做 token 预算与并发限制，注册现有 `TaskRepository`/`TaskWorker` 的 `MATCH_REVIEW` handler 并持久化进度，不占搜索 4.5 秒 deadline。模型不可用时返回可理解状态，不降级为“推荐”。
- [ ] 复核 prompt 只给 JD 核心职责、已召回候选人的技术栈/相关工作/项目证据，要求逐项引用来源、指出缺口并允许“证据不足”。后端对引用做 revision/字段校验；不得让 AI 推翻确定性硬拒绝。测试日志中无简历正文/密钥。
- [ ] 运行 `python -m pytest tests/match/test_ai_review.py tests/api/test_match_review.py tests/db/test_match_review_schema.py tests/providers -q`；用脱敏样本测 50 和 100 对的总时延、输入输出 token、失败恢复与取消。

### Task 13：AI 复核前端控制、进度和人工覆核

**Files:** 修改 `desktop/src/api/client.ts`, `desktop/src/App.tsx`, `desktop/src/pages/JdManagementPage.tsx`, `desktop/src/pages/TalentPoolPage.tsx`；测试 `desktop/src/pages/JdManagementPage.test.tsx`, `desktop/src/pages/TalentPoolPage.test.tsx` 及可用的 Playwright 流程。

**Interfaces:** 两个匹配入口都先返回基础结果；结果页的“AI 深度复核”默认关闭，用户手动触发 Task 12 API。界面并列显示基础匹配分/规则结论、AI 建议与引用证据；复核中的取消、重试和部分失败可见，人工处理状态沿用现有流程。

- [ ] 写失败用例：默认点击“匹配”仅发基础请求；打开复核才 POST review；结果页关闭后重开能恢复任务状态；AI 失败/部分完成时基础结果可继续查看、建流程；“待核”不自动当作“推荐”。
- [ ] 运行聚焦 Vitest 确认失败，接入任务状态轮询/取消和证据展示；避免把 100 份简历塞进同步 HTTP 请求或浏览器状态。默认展示约 30 项且允许继续浏览，不做硬性的最终人数上限。
- [ ] 运行 `npm test -- --run`, `npm run build`, `npm run test:e2e -- --list`；在可运行的本机环境做真实 UI 冒烟：JD→候选人、候选人→JD、复核开/关、取消、失败、编辑后重跑。

## 阶段 6：最终验收、索引迁移与发布

### Task 14：同快照对照、回归与回滚演练

**Files:** 更新 `backend/tests/fixtures/search_quality_v2.json`, `backend/tests/fixtures/match_quality_v2.json` 的匿名标签；新增 `docs/verification/search-match-quality-2026-09-16.md`；如索引版本变化，更新 `backend/src/kerui_recruit/search/rebuild_maintenance.py` 对应测试与发布脚本说明。

- [ ] 锁定验收集只跑一次最终对照：旧版与新版同模型、同数据、同 Top K；报告搜索 `Recall@50/100`、`Precision@10`、`nDCG@10`、字段误伤、p50/p95；匹配 `Precision@5/10`、方向/技能/年限硬条件违规、职责证据正确率；AI 开/关的增益、时延和费用单列。若未改善，回滚相关排序/向量变体，不以调高阈值或删难例掩盖。
- [ ] 在隔离索引检查 schema/chunk/embedding 元数据、记录数、当前 revision、编辑/删除/新导入 outbox；若需切换，先 staging 验证、停应用、保留原索引、切换后复查，演练恢复原索引。不要在活跃 `.dev-data` 上做破坏性重建。
- [ ] 后端运行 `python -m pytest -q`；前端运行 `npm test -- --run`、`npm run build`、可运行的桌面冒烟。记录命令、通过数、失败 node ID 和环境限制，不写“全部通过”而无命令输出。
- [ ] 最终门槛：必过误筛/正文/AND-OR/双向一致/AI 关闭用例全通过；硬条件违规 0；独立集主要指标不退化且至少一个预先指定指标改善；p95 在约定交互预算内；用户可从前端清晰区分基本匹配、AI 结论和待核。

## 两个业务提示词的实施合同

Task 7 的方向提示词应以如下判定次序编写，并同时用于 JD 与简历的**独立分类步骤**；不要在大段解析提示词里只加一句“Agent 归后端”。具体输出字段可随 Pydantic 类型命名调整，但含义和校验不能删：

```text
任务：只判断职业方向，不补写原文没有的经历。先找主要交付物和最近/最核心的职责，
再看项目证据，最后参考职位名。技术名词、公司名、学历和 AI 分类不能单独决定方向。
BACKEND：服务端/API/业务系统/数据平台与管道开发/Agent 应用编排和工具集成。
DATA：数仓分析、BI、指标体系、分析建模及数据分析交付。
ALGORITHM：模型训练、算法研发、推荐/搜索算法、推理算法优化。
FRONTEND、OPS、QA、PRODUCT、MANAGEMENT 按各自主职责；明确不属于上述方向用 OTHER。
数据工程按主要职责区分；Agent 应用工程与训练模型的算法工程分开。
职责相当、信息不足或相互冲突时 direction=null，confidence=low，进入待核。
仅返回 JSON：{"direction":合法枚举或null,"confidence":"high|medium|low",
"evidence_paths":[结构化字段路径],"reason":"一条简短事实说明","taxonomy_version":"1"}。
evidence_paths 必须指向输入中实际存在的字段；不能引用推测的职责。
```

Task 12 的 AI 复核提示词必须让模型逐条核查 JD 核心职责和候选人证据，不允许只根据技术栈的词面重合给“推荐”。业务提示词最小合同：

```text
你在复核一个已完成确定性资格判断的 JD—候选人配对。输入只有当前 JD revision
的核心职责/必需技能，以及当前简历 revision 的技术栈、工作和项目证据片段及 ID。
逐项判断职责是否有直接证据、弱证据或无证据；指出必需技能的证据与缺口。
不得假设未写出的能力、使用公司或学校光环补足证据、改变方向/年限等硬条件。
证据不足时用 pending；有清楚的职责和技能证据才用 recommend；明显不符用 reject。
仅返回 JSON：{"verdict":"recommend|pending|reject","duty_checks":[
{"jd_duty_id":"输入中的 ID","status":"direct|weak|missing",
"resume_evidence_ids":["输入中的 ID"]}],"missing":["简短缺口"],
"reason":"两句以内的事实性理由"}。
只允许引用输入 ID；不要输出姓名、手机号、简历全文或不存在的经历。
```

上线前用标注边界集逐项验证这两份提示词；示例是**输出合同**，不是准确率保证。应用 provider 若实际不支持稳定 JSON，后端仍须严格校验，失败按待核/未完成处理。

## Task 级汇报格式与暂停条件

每完成一个 Task，在 `.trae/search-match-quality-progress.md` 追加：日期、Task 编号、读取到的实际接口、修改文件、RED 与 GREEN 命令/结果、样本指标、与计划差异、是否可回滚。存在以下情况时停在当前任务并报告：真实数据或密钥可能外泄；索引版本不兼容或活跃库写入风险；人工标注不足以判断质量；方向人工修订可能被覆盖；硬条件违规；旧 API 调用者断裂；AI 复核与基础结果混淆；测试失败原因未确定。修正该任务后继续，不用一次性重新规划整份方案。
