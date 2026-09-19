# 检索与双向人岗匹配准确度改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. This project is not a Git repository; save a file-level change list and test output at each checkpoint instead of inventing commit commands.

**Goal:** 修复已实测的纯向量空结果、职业方向造成的漏检、技能误硬筛及同方向误荐，使 JD 找人与人找 JD 均按真实职责和项目证据给出可信结果。

**Architecture:** 保留现有搜索与匹配 API。解析层输出主方向、第二方向、置信度及来源，并把 JD 的真正必备技能表示为 AND/OR 组；匹配层采用同向召回加跨方向救援召回，共用一份候选人证据和资格判断，再按职责、项目、技能和方向重排。纯向量阈值与索引文本在隔离副本上消融后再决定生产改动。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLAlchemy/SQLite、LanceDB、现有 SiliconFlow embedding/reranker、React/TypeScript/Tauri、pytest、Vitest。

**Spec:** `docs/verification/2026-09-17-semantic-audit.md`。本计划以 2026-09-17 当前源码为准；旧文档仅用于历史背景。

## Global Constraints

- 本次是阶段性准确度改造，不引入本地模型训练、新向量数据库或大规模界面重做。
- 搜索框的用户手选职业方向仍是精确筛选；改动的是两个“匹配”入口自动推断出的方向规则，不能悄悄改变用户明确筛选条件。
- 数据工程按主交付区分：数据平台/采集处理服务偏 BACKEND；数仓主题建模/指标/BI 偏 DATA。Agent 应用编排偏 BACKEND，模型训练/算法优化偏 ALGORITHM。
- “管理”同时记录为职责属性，不能覆盖有证据的技术/产品专业。沿用现有 `direction` 字段兼容老数据与前端。
- AI 深度复核仍由用户显式点击，默认关闭；普通匹配不得自动发起 DeepSeek 生成复核。
- 只有 JD 原文明确写出的必要资格/技能可以成为硬门槛；“或”在组内取其一，多个独立组之间才取 AND。不能让无职责、无技能证据者靠总分进入“推荐”。
- 保留人工修改优先级。回填先 dry run，再分批写入并同步索引；每个批次可从备份恢复。
- 评测只能输出匿名编号和脱敏证据，不在报告或日志中写姓名、电话、邮箱、原始简历或 API 密钥。

## 文件职责与实施顺序

| 改动单元 | 主要文件 | 输出 |
| --- | --- | --- |
| 固定评测 | `scripts/evaluate_semantic_quality_v3.py`、`backend/tests/fixtures/*quality_v3.json` | 相同数据、标签和指标可重复跑 |
| 方向结构与解析 | `direction/policy.py`、`direction/classifier.py`、`providers/generation_tasks.py`、`resumes/structured.py`、`resumes/normalize.py`、`jd/structured.py` | 主/次方向、置信度、证据路径、管理属性 |
| JD 硬条件 | `jd/structured.py`、`match/policy.py`、`match/service.py` | 可解释的 MUST AND/OR 组与兼容旧 JD 的规则 |
| 候选人统一证据 | 新建 `match/candidate_view.py`；修改 `match/service.py`、`search/sync.py` | 多份当前简历仍有一致的方向、技能和职责视图 |
| 召回与排序 | 新建 `match/recall.py`；修改 `match/service.py` | 同向 + 跨向救援、职责项目证据排序、双入口资格一致 |
| 向量消融 | `search/service.py`、`search/documents.py`、`search/sync.py`、`search/rebuild_maintenance.py` | 仅在指标支持时调整阈值或向量文本 |
| API/桌面展示 | `api/match.py`、`desktop/src/api/client.ts`、`desktop/src/App.tsx`、`desktop/src/pages/JdManagementPage.tsx` | 推荐/待核依据、合理默认返回量、AI 按钮维持可选 |

---

### Task 1：冻结可重复的业务评测门槛

**Files:** Create `scripts/evaluate_semantic_quality_v3.py`; create `backend/tests/fixtures/search_quality_v3.json` and `backend/tests/fixtures/match_quality_v3.json`; reference `.semantic-audit-snapshot/*` read-only.

**Interfaces:** 输入固定查询编号、候选人/JD 匿名别名、0–3 相关级、来源和人工复核状态；输出 JSON 指标与 Markdown 差异表。匿名别名运行时从冻结 SQLite 映射，不把原始 UUID 放进报告。测试夹具的最小结构如下，`adjudicated_grade` 在复核前允许为 `null`，不得把模型分数自动复制进去：

```json
{"case_id":"Q04","query_type":"candidate_search","query":"Agent 应用开发","hits":[{"alias":"C001","model_grade":3,"headhunter_grade":null,"adjudicated_grade":null,"evidence_paths":["projects[0].summary"]}]}
```

- [ ] 从 24 个搜索意图、10 个 JD、5 位候选人及 52 个方向边界卡片生成固定用例。保留独立模型原判；对搜索两模式前 5、当前已返回的 19 个匹配配对（14 个正向、5 个反向）以及报告明确指出的漏检配对、52 个方向卡片按报告中的猎头规则人工复核，分歧写证据，不能以模型自评自动定真值。
- [ ] 先让评测脚本在当前代码/索引上复现：纯向量 6/24 空、模型判定宏 P@5=0.375；混合 P@5=0.8583；10 个 JD 返回 14 对；5 位候选人返回 5 个岗位。脚本检查样本是否来自同一快照和索引版本，防止混用。
- [ ] 评测结果分别列出 `model_grade`、`headhunter_grade`、`adjudicated_grade` 与理由；指标默认使用复核后的标签，模型指标另列。对所有新策略输出 P@5、P@10、nDCG@10、已知相关对 Recall@50/100、强相关误荐数、已知漏检数与请求耗时。P@5 的分母固定为 5，空位按 0 计，强相关定义为 grade=3，相关定义为 grade≥2；Recall 只能称“已知相关对召回率”，不能冒充全库召回率。先对原始结果和 50–100 人候选池取标签，后续每轮把新出现的前排配对补判，防止只评旧结果而放过新误荐。
- [ ] 运行 `py -3.12 scripts/evaluate_semantic_quality_v3.py --snapshot .semantic-audit-snapshot --baseline`；确认输出数值与上一节一致，且 JSON/Markdown 无姓名、联系方式。基线不一致时先定位索引或模型版本差异，不继续改算法。

**Gate 1:** 基线可重复、标签可追到脱敏职责/项目、零敏感信息外泄。

### Task 2：把职业方向变为有证据的主/次判断

**Files:** Modify `backend/src/kerui_recruit/direction/policy.py`, `direction/classifier.py`, `providers/generation_tasks.py`, `resumes/structured.py`, `resumes/normalize.py`, `jd/structured.py`; test `backend/tests/direction/test_classifier.py`, `backend/tests/resumes/test_pipeline_direction.py`, `backend/tests/jd/test_jd_pipeline_direction.py`（若文件不存在则在相应目录创建）。

**Interfaces:** 继续输出旧 `direction`。新增 JSON 字段 `direction_assessment`：`primary: Direction | None`、`secondary: Direction | None`、`confidence: high|medium|low`、`evidence_paths: list[str]`、`management: bool`、`taxonomy_version: "2"`。路径例为 `experiences[0].summary`，不要复制整段含个人信息的正文。避免使用旧升级程序会清理的键名 `direction_profile`。

首版职业判断表如下。每类均以最近职责和实际交付为主，技术名只作辅助证据；分类器必须把命中的证据路径写进结果。

| 方向 | 主要交付证据 | 不足以单独判定的词/边界 |
| --- | --- | --- |
| BACKEND | 服务端 API、交易/业务系统、数据采集处理服务、数据平台基础设施、Agent 应用编排和工具调用 | 只会 Python、SQL、LangChain；以数仓指标建模为主应偏 DATA，以模型训练为主应偏 ALGORITHM |
| FRONTEND | Web/移动客户端交互、组件体系、前端构建与性能、iOS/Android 客户端 SDK | 只会 JavaScript；服务端 BFF/API 为主时可列 BACKEND 次方向 |
| ALGORITHM | 模型训练/微调/评估、推荐/搜索算法优化、特征与推理效果交付 | 使用现成大模型 API 搭 Agent 应用、只写提示词 |
| DATA | 数仓分层/主题建模、指标体系、BI/分析、数据质量治理中以数据资产为交付 | 只会 Spark/Flink/SQL；以平台 API/调度服务开发为主应偏 BACKEND |
| OPS | 环境与发布、云原生平台、SRE 稳定性、监控和基础设施自动化 | 只使用 Docker/K8s 开发业务服务 |
| QA | 测试策略、自动化测试平台、质量工程和缺陷预防 | 开发经历里只提到单元测试 |
| PRODUCT | 需求发现、产品规划、流程/指标设计、产品效果负责 | 研发负责人参与需求评审或数据治理工程实施 |
| MANAGEMENT | 只有管理交付占主导、缺少可识别的专业交付时作为主方向；其他情况优先用 `management=true` 属性 | 团队人数、职级、负责人头衔本身 |
| OTHER | 有明确职责证据，但确实不落入以上类，例如纯销售/招聘 | 解析失败、职责缺失或方向不确定，应保留 null/low |

```python
# 边界回归，断言职责和项目胜过技能表中的孤立词。
assert classify_direction(data_platform_with_api_and_warehouse_terms).direction == "BACKEND"
assert classify_direction(warehouse_modeling_and_metrics).direction == "DATA"
assert classify_direction(agent_tool_orchestration).direction == "BACKEND"
assert classify_direction(model_training_and_evaluation).direction == "ALGORITHM"
```

- [ ] 先写边界测试：近期以数据平台/API 交付为主且同时有数仓词 → BACKEND 主、DATA 次；以数仓模型/指标为主 → DATA 主；LangGraph 工具调用 → BACKEND；模型微调/推荐算法 → ALGORITHM；iOS 客户端 SDK → FRONTEND；研发负责人同时有 Java 架构交付 → BACKEND 主且 `management=true`；仅技能表列有词但缺职责/项目 → low。
- [ ] 用统一评分口径让解析器输出证据：候选人最近职责 50、个人项目产出 30、职位名 10、技能 10；JD 核心职责 60、可验交付 25、标题 10、工具 5。最高类 ≥70 且领先第二类 ≥20、至少两类独立证据才为 high；分差不足 20 保留 `secondary`。这些分数是待用 52 张边界卡片校准的初值，只用于解释和测试，不伪称概率；测试必须覆盖单一来源重复出现不能算两类独立证据。
- [ ] 修改简历与 JD 提示词，列明八类方向的决定性交付与排除例。原文模糊时仍给展示用最可能主方向，但标 low 并给次方向；禁止用“出现一个关键词即 high”或把管理职责覆盖专业方向。`OTHER` 只表示明确不在当前方向体系，不能代替解析失败。
- [ ] 把结构透传至 `NormalizedResume` 和 JD `parsed_data`；保留手工 `direction` 覆盖优先。确定性回退分类器按职责/项目/技能分别计分，单个词至多 low，不再按 ALGORITHM→DATA→BACKEND 的固定优先级直接返回 high。
- [ ] 在 52 个脱敏边界样例上输出新旧标签、证据和分歧列表。高置信的错误归类不得包含报告中 iOS→BACKEND、Agent 应用→ALGORITHM、数据平台→DATA 这三类；与复核标签的一致率目标 ≥90%，但不通过简单“全判 low”刷指标。
- [ ] 评估是否新增顶层方向：逐个统计 52 张卡片及新增误检样例中无法由“主/次方向 + 管理属性”表达的职业簇。只有职责/交付明显独立、至少 5 个已复核案例且现有分类确实导致检索错误时，才提出新增类别和迁移成本；否则先用专业标签（如 Agent 应用、数据平台、数仓）表示，避免在小样本上扩张枚举。

**Gate 2:** `direction` 向后兼容，三类已知边界不再高置信误分；管理可与专业并存。此时不大批量回填、不改变匹配召回。

### Task 3：重建 JD 必备技能的语义边界

**Files:** Modify `backend/src/kerui_recruit/jd/structured.py`, `providers/generation_tasks.py`, `match/policy.py`, `match/service.py`; test `backend/tests/match/test_match_policy.py`, `backend/tests/match/test_match.py`。

**Interfaces:** 新 JD 增加 `must_skill_groups: list[{alternatives: list[str], source_quote: str}]`。组间 AND、组内 OR；`source_quote` 必须能在 JD 原文中定位。`required_skills` 保留给旧记录和排序，不再把前 5 个无差别当作全部硬条件。`evaluate_pair(jd_parsed, candidate_parsed)` 继续返回 `PairDecision`，增加逐组 `matched/missing` 与职责证据。

```json
{"must_skill_groups":[{"alternatives":["LangGraph","CrewAI"],"source_quote":"熟悉 LangGraph 或 CrewAI"},{"alternatives":["Python"],"source_quote":"必须熟练使用 Python"}]}
```

上述示例表示 `(LangGraph OR CrewAI) AND Python`；若 JD 原文只把框架列为“例如”，该框架组不能出现在 `must_skill_groups` 中。

- [ ] 先写四个失败回归：前端人选有组件/构建工程项目时，不因缺字面“前端工程化”被拒；数仓人选有 MySQL/SQL/数仓项目时，不因缺字面“关系型数据库”被拒；“LangGraph 或 CrewAI”任一命中即满足该组；没有任何岗位职责或相关技能证据的候选人不能被推荐。
- [ ] JD 提示词只把原文明确表达的必要技能放进 `must_skill_groups`，将工具示例、可替代框架、泛能力放加分项；校验组内词不为空、引用原文可定位、重复词合并。旧 JD 的 `required_skills` 作为软特征；若没有新组，技术岗位至少要有一项岗位专属技能或直接职责证据，否则标待核/排除。
- [ ] 在一个模块实现技能同义与能力证据匹配，覆盖关系型数据库↔MySQL/PostgreSQL/Oracle、前端工程化↔组件库/构建工具/微前端等具体项目证据；不把所有数据库或所有前端词视为同义。正向、反向调用同一 `evaluate_pair`，删掉 `service.py` 中与之冲突的另一套 MUST 判法。
- [ ] 运行 `py -3.12 -m pytest tests/match/test_match_policy.py tests/match/test_match.py -q`（工作目录 `backend`），再跑冻结配对：f96cb81456e3 对 d13894a1 应可入选；42c0b000fc46 对 56f942a1 应至少待核；9eeb2bf3737c 对 543afa81 只能待核，不能因术语接近直接推荐。

**Gate 3:** 明确硬条件没有被分数补偿；通用词/可替代工具不再造成上述漏检。

### Task 4：统一多份简历的人选证据

**Files:** Create `backend/src/kerui_recruit/match/candidate_view.py`; modify `match/service.py`, `search/sync.py`; test `backend/tests/match/test_reverse_index.py`, `backend/tests/search/test_sync.py`。

**Interfaces:** `build_candidate_view(revisions: list[ResumeRevision], candidate: Candidate) -> dict`：按 `created_at desc, id desc` 选最近修订的结构化硬字段/主方向，合并所有当前 READY 修订中去重后的技能、经历和项目，保留来源修订 ID。正反向匹配都消费此视图；索引的同一候选人各 chunk 使用同一候选人级方向硬字段。

- [ ] 先用两份当前简历造测试：旧版缺 SQL/Python，新版有 SQL/Python 和数仓项目；无论 SQL 查询顺序、命中哪个 chunk，资格判断与展示应使用同一人选视图。
- [ ] 替换 `_candidate_parsed_data` 的无序字典覆盖；让 `_candidate_representation`、正向评分和反向评分使用同一个视图。原始经历可以仍分 chunk 检索，但不能出现旧 chunk 的方向与最新解析方向相互矛盾的硬筛。
- [ ] 同步索引时对同一候选人的各 chunk 写统一主方向/年限/学历/地点，并保留每个 chunk 的文本来源；先在隔离索引重建，验证候选人去重与 `revision_id` 可追溯。
- [ ] 运行 `py -3.12 -m pytest tests/match/test_reverse_index.py tests/search/test_sync.py -q`，重点看多修订候选人、人工改字段后的同步和正反向同配对资格。

**Gate 4:** 现有多简历候选人不因命中不同修订而出现相反资格结论。

### Task 5：双通道召回与职责证据重排

**Files:** Create `backend/src/kerui_recruit/match/recall.py`; modify `match/service.py`, `match/policy.py`, `api/match.py`; test `backend/tests/match/test_match.py`, `test_reverse_index.py`, `test_match_policy.py`, `backend/tests/api/test_jd_flow.py`。

**Interfaces:** `recall_for_jd(...)` 和 `recall_for_candidate(...)` 返回去重后的 50–100 个候选配对及 `recall_lane="same_or_adjacent"|"rescue"`。`evaluate_pair` 返回资格与依据；最终 `MatchScore` 增加 `match_tier="recommend"|"needs_review"` 和简短证据/缺口。两入口的同一人岗配对资格必须一致，排序分可因查询方向不同而略有差异。

```python
def merge_recall_lanes(same_hits, rescue_hits, *, entity_id, limit=100, rescue_slots=30):
    """按候选人 ID 或 JD ID 去重；预留救援位，未满时由另一通道补足。"""
```

正向以 `candidate_id` 去重，反向以 `jd_id` 去重。`rescue_slots` 是召回预算，不是最终展示数量或评分加成。

- [ ] 先写失败测试：具体 FRONTEND JD 能从 rescue 通道找到方向空值的三位前端人选；DATA 工程 JD 可以评估 BACKEND 数据平台候选人；用户手动选择 `direction=FRONTEND` 的**搜索框精确过滤**仍只返回该方向；真正不相关的 PRODUCT↔算法配对不因放宽方向被自动推荐。
- [ ] 把匹配用硬过滤拆成“真实硬条件（年限、学历、地点、显式排除）”和“方向同向通道”。同向/次向通道先取约 70，取消方向过滤的救援通道再取约 30 个不重复者；不足时动态补足，总池约 50–100。两通道共用一次请求时限，重复命中只保留最有证据的 chunk，不准固定截断到 30 后再筛。
- [ ] 改方向规则：同向高分、次向/相邻方向中分、低置信方向不处罚；仅双方高置信且近期职责/项目都证明不相容时拒绝。方向暂按 10% 排序因子，先保留可配置值。加入职责与项目证据核查；试验排序初值为职责 35%、明确技能 25%、项目业务场景/规模 20%、资历 10%、方向 10%，不把每个分量的原始量纲直接相加。
- [ ] 用现有 reranker 对 JD 核心职责与候选人最近职责/代表项目的脱敏组合文本批量重排，记录引用的经历/项目 ID；模型不可用时用已验证的词级证据降级并标记，不伪造语义分。数据分析 JD 的大数据平台/治理产品两人不得进入“推荐”；算法/Java 直接相关人选仍保持在前排。
- [ ] API 返回 `match_tier`、`direction_reason`、`evidence`、`missing` 与真实 `pool_size`；“待核”可以展示但不能混在“推荐”里。双向保存同一 `evaluate_pair` 判定。运行上述测试并复核 10 JD、5 候选人固定样例。

**Gate 5:** 前端 JD 至少救回三位直接相关者；数仓直接相关者可出现；两位明显不合适的数据分析人选与两个前端岗误荐不在推荐层；AI 关闭仍能给出证据分层结果。

### Task 6：校准纯向量，不凭感觉删字段

**Files:** Modify `backend/src/kerui_recruit/search/service.py` only if threshold experiment wins; modify `search/documents.py`, `search/sync.py` only if A/B/C index实验 wins; use `search/rebuild_maintenance.py` for isolated rebuild; test `backend/tests/search/test_service.py`, `test_documents.py`, `test_hybrid.py`。

**Interfaces:** 纯向量与混合向量通道调用同一个 `apply_vector_threshold`；阈值参数可追踪到索引/评测版本。不得把关键词结果悄悄混入用户选择的“纯向量”模式。

- [ ] 在冻结索引上记录每个查询阈值前前 100 的原始相似度、阈值后数量及标签；对绝对阈值 0.45/0.50/0.55/0.60 和相对比率 0.85/0.90 做网格对比。先查 Q04 Agent、Q13 React 空结果是否确由阈值所致。
- [ ] 用隔离索引比较现有 A（画像+学校/城市/年限/公司/技能）、已有 B（父向量去学校/城市/年限）、已有 C（B 加项目名/业务场景）及 JD 向量 B。只比较同一 embedding/reranker、同一 24 查询与同一标签，记录索引体积、embedding 成本及耗时。
- [ ] 只有方案满足：以同一批复核标签计算的纯向量 P@5 ≥0.60、Q04/Q13 有直接相关结果、24 查询中“有已知相关人选却空结果”≤1，且混合 P@5 不低于 0.85、Q19 P@5 至少 0.6，才切换生产向量阈值/文本。若 A/B/C 无一达标，保留当前文本，把差距写入报告，继续通过混合检索和职责重排解决用户主流程。模型标签的旧基线与复核标签的新指标不得直接相减。
- [ ] 获胜方案先在隔离索引全量重建，再按既有 `rebuild_maintenance` 流程切换；保留旧索引和配置以便回退。跑搜索聚焦测试及 24 查询对照，不把“返回更多”本身当作准确度提升。

**Gate 6:** 纯向量和混合改善均有同标签证据；没有证据则不动生产索引。

### Task 7：前端展示、批量回填与阶段验收

**Files:** Modify `desktop/src/App.tsx`, `desktop/src/pages/JdManagementPage.tsx`, `desktop/src/api/client.ts`, `desktop/tests/App.test.tsx`; modify `direction/backfill.py`; write `docs/verification/2026-09-17-search-match-v2-acceptance.md`。

- [ ] 前端按“推荐 / 待核”分组展示简短职责或项目证据、缺口及跨方向原因；显示实际返回人数。JD 找人后端先从 50–100 人池完成排序，单次返回最多约 100 条，前端初始只展开前 30 条；点击“查看更多”展开同一次匹配的剩余结果，不重新检索、不改变排序或匹配运行 ID。更新当前 `runJdMatch(..., 1000)` 和空结果提示；方向缺失不提示用户必须逐条确认才能匹配。此项只改变匹配结果的展示数量，不改变人才库搜索框默认首批 50 条的设置。
- [ ] 保留两入口的“AI 深度复核”显式按钮与失败/重试状态。前端测试断言：初次点击匹配不会发起 AI 生成复核；只在点击复核后调用；复核失败不清空基础结果；“待核”不显示为“推荐”。
- [ ] 在 `.dev-data` 与索引备份后，扩展现有 `direction/backfill.py`：当前它只复判 null/OTHER/非法值，无法修正合法但错误的旧方向；新增 `audit_all_non_manual` dry run，对全部非人工覆盖记录报告拟改动数/冲突数/证据等级/人工覆盖数。实际写入分批，先改空/OTHER，再对有双来源证据的高置信误分复核后改写，并触发索引同步。禁止把全部旧简历重新生成画像来完成方向修复；低置信仍能走救援召回。
- [ ] 运行后端聚焦测试：`py -3.12 -m pytest tests/search tests/match tests/direction tests/api/test_jd_flow.py tests/api/test_direction_pending.py -q`（工作目录 `backend`）；运行前端 `npm test` 与 `npm run build`（工作目录 `desktop`）。已有不相关失败必须单列，不得用整体“通过”掩盖。
- [ ] 运行 Task 1 固定评测与 2000–5000 份规模的 20 次匹配请求，记录 P50/P95 耗时、超时和服务错误；当前冻结快照只有 1713 条简历修订，规模试验可在隔离副本复制匿名记录，仅测耗时与稳定性，复制数据绝不能进入准确度评测。目标是相对当前基线 P95 不增加超过 20%，且不突破当前 8 秒单请求预算。逐项写明 Gate 1–6 的新旧数值和未达标原因。

**最终验收:** 所有已知直接相关前端/数仓漏检被救回；已知同方向数据分析误荐不再作为推荐；双向同配对资格一致；纯向量与混合达到 Gate 6；AI 复核默认关闭；现有搜索精确筛选、正文开关和用户显式方向过滤无回归。当前人才库搜索框只取首批最多 50 条且没有加载下一页的界面，本计划的 Task 7 不把它误称为“现有分页”。若 A/B/C 均未过 Gate 6，保留旧生产索引并继续做查询表达/字段权重或多向量候选池试验，不能把“实验完成”写成“准确度完成”。

## 给 Trae「任务1」的执行提示词

> 请在当前 `candidate_pool` 工作区执行 `docs/superpowers/plans/2026-09-17-search-match-accuracy-v2.md`，先读 `docs/verification/2026-09-17-semantic-audit.md` 和当前源码；旧文档过时处以当前源码为准。按 Task 1→7 顺序一次只做一项，每项先写针对实际漏检/误荐的失败测试，再改最少代码、跑聚焦测试、把改动文件、测试结果、固定样例前后对比写到 `.trae/search-match-accuracy-v2-progress.md`。不要把 DeepSeek v4pro 的分类或自评当作真值；遇到模型与职责证据冲突时列出脱敏依据并按猎头规则复核。不要自动改生产向量索引，先在冻结快照做阈值与 A/B/C 消融；不要扩大到与准确度无关的企业级改造。两种匹配入口必须对相同配对给出相同资格结论；方向不确定者进入救援召回与待核层，明确用户手选方向的搜索仍是硬筛。AI 深度复核保持前端可选且默认关闭。每项 Gate 未通过先修当前项，不跳过。最后交付验收报告和可复现命令，明确仍未达标的内容。
