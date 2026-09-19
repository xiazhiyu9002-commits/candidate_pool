# 双向严格人岗匹配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把两个「匹配」按钮改为同一条严格准入流程，只返回行业、职业方向、核心技能和职责经历均有证据支持的人岗组合；末尾增加手动触发的可选 LLM 分析。

**Architecture:** 规则层输出统一的准入决定和证据，检索层只负责提供候选池与职责片段，重排序只处理已通过准入的对象。JD→候选人与候选人→JD 共用规则层。LLM 分析通过现有持久任务队列异步运行，与在线匹配解耦。

**Tech Stack:** Python/FastAPI/SQLAlchemy/LanceDB、React/TypeScript/Tauri、pytest、现有 embedding/reranker 和 TaskRepository/TaskWorker。

**Spec:** `docs/superpowers/specs/2026-09-15-strict-dual-match-design.md`

## Global Constraints

- 只更改两个匹配入口；人才库搜索框的三种检索保持原行为。
- 两个按钮没有关键词、向量、混合模式选择；API 对旧 `mode` 参数可暂时兼容但忽略并记录弃用，客户端不再发送。
- 行业、方向和所有明确硬条件缺失或不相容时失败关闭；服务错误与「无合适结果」分开响应。
- JD 核心技能组目标数量为 3～5、上限 5，须在当前 JD 版本上由业务人员核对；原文真正只有 1～2 个 MUST 时允许保留 1～2 个，不发明关键词。PLUS 不进入组，MUST 中未选入窗口的明确资质仍须检查；技能 AND/OR 与词边界严格。
- 匹配列表仅展示证据充分的对象；向量分、rerank 分、LLM 结论不能绕过准入规则。
- LLM 分析仅手动触发、异步运行、按简历/JD 版本及模型身份缓存；默认关闭，不影响主路径耗时。
- 本目录没有 `.git`，当前副本不能执行提交步骤；实施时若另有 Git 工作树，按可测试任务分批提交。

## 文件职责

| 文件 | 职责 |
|---|---|
| `backend/src/kerui_recruit/match/strict_rules.py`（新） | 行业、方向、硬条件、核心技能准入及证据结构；两个入口共用 |
| `backend/src/kerui_recruit/match/core_skills.py`（新） | 离线挑选 3～5 个专业技能组、别名及 AND/OR 解析 |
| `backend/src/kerui_recruit/match/duty_evidence.py`（新） | JD 职责与简历工作/项目片段的向量核对及来源记录 |
| `backend/src/kerui_recruit/match/service.py` | 调度两个入口的统一准入、去重、排序和落库 |
| `backend/src/kerui_recruit/search/industry.py` | 有版本的行业大类/细类、受控别名和行业准入；职业方向沿用现有统一口径 |
| `backend/src/kerui_recruit/search/contracts.py`、`search/lancedb_index.py`、`search/documents.py`、`search/sync.py`、`match/jd_index.py` | 建立可核实职责/经历的子片段及行业、方向元数据索引 |
| `backend/src/kerui_recruit/jd/structured.py`、JD 核对 API 与界面 | 存储当前 JD 版本上经业务人员核对的核心技能组 |
| `backend/src/kerui_recruit/api/match.py` | 新的匹配状态、证据响应和可选分析 API |
| `desktop/src/App.tsx`、`desktop/src/api/client.ts`、`desktop/src/pages/TalentPoolPage.tsx`、`desktop/src/pages/JdManagementPage.tsx` | 两个按钮统一入口、取消模式切换、展示准入证据和空/错误状态 |
| `backend/src/kerui_recruit/match/llm_analysis.py`（可选新文件） | 单条匹配的异步 LLM 分析、引用和版本缓存 |

## Task 1：冻结样例和字段合同

**Files:** `backend/src/kerui_recruit/bench/search_acceptance.py`、`docs/十种选人方式量化验收与Trae执行要求.md`、新建 `docs/verification/strict-match/` 下的标注数据与报告。

**Produces:** 每对人岗的标签、原始证据、硬条件判定、两方向的相同准入期望；开发集用于阈值，独立集只验收。

- [ ] 从当前数据库选真实 OPEN JD 与 AVAILABLE 简历，冻结版本 ID；两个方向都覆盖后端、前端、算法、数据、运维、测试、产品、管理等岗位，以及行业相同但方向不同、方向相同但行业不同、技能重叠但职责不同的负例。
- [ ] 招聘人员只根据原始简历/JD 标注 `合适/不合适/证据不足` 和每项来源，先不看现有分数或系统理由；留出独立验收集，不在其上调阈值。
- [ ] 编写评测输出：返回项中不合适数、硬条件违规数、证据错误数、空结果用例、漏掉合适数、P95、依赖失败数。先运行旧两个按钮作真实基线，说明旧十种方式中的双向六项指标将被新双入口合同替代。

## Task 2：统一行业、方向及硬条件准入

**Files:** 新建 `match/strict_rules.py`；修改 `search/industry.py`、`match/service.py`；测试新建 `backend/tests/match/test_strict_rules.py`。

**Interface:** `decide_hard_gates(jd_data: dict, candidate_data: dict, *, years: float | None, degree: str | None, locations: tuple[str, ...]) -> MatchDecision`，`MatchDecision.passed` 与 `reasons` 已在 `match/service.py` 定义；证据结构另含字段值、来源片段 ID 和版本 ID。

- [ ] 先写失败测试：现有职业方向口径判为相容且有真实行业证据时通过；`OTHER`、空行业、无项目/工作行业证据、现有口径判为不同方向、宽泛「IT/软件」导致的假同类、年限/学历/地点/排除项缺失均不通过；JD 仅要求「金融」可接受银行/证券/保险的有证据经历，明确要求「银行」不能被证券/泛金融经历冒充；两边交换入口得到同一决定。运行 `python -m pytest tests/match/test_strict_rules.py -q`，确认失败。
- [ ] 从现有 JD/简历行业自由文本统计原值和高频别名，建立有版本、稳定代码的「行业大类 → 细分行业」表，原文本映射时同时保存代码、原始值、来源与是否已人工核对。收紧 `industry.py` 中过宽的子串别名，避免 `IT`、`软件`、`财务` 等无上下文词直接提供行业通过证据；职业方向只消费现有统一分类结果，不修改其 taxonomy。只从简历 `experiences[].industry`、项目 `business_scene` 或明确业务经历片段取行业证据，单独的公司行业字段不放行。
- [ ] 在 `strict_rules.py` 实现失败关闭：任一明确条件没有值或来源、值冲突、方向/行业未知均 `passed=False`；沿用当前 JD 工作地可由现居或明确意向城市满足的合同。测试转为通过，并回归现有 `backend/tests/match/`。

## Task 3：3～5 个核心技能组

**Files:** 新建 `match/core_skills.py`；修改 `match/service.py`、`search/sync.py`、`jd/structured.py`、`api/jd.py`、`desktop/src/pages/JdManagementPage.tsx`；测试新建 `backend/tests/match/test_core_skills.py`。

**Interface:** `select_jd_core_groups(parsed_jd: dict) -> tuple[SkillGroup, ...]`、`select_candidate_keywords(parsed_resume: dict) -> tuple[str, ...]`、`check_core_groups(groups, parsed_resume) -> SkillDecision`。`SkillGroup` 保存原文、规范词、`ALL/ANY` 关系与 JD 版本；`SkillDecision` 保存命中、缺失及简历来源。

- [ ] 先写失败测试：每侧最多 5 个专业词；软技能与 PLUS 不占窗口；同义别名受控；Java 不命中 JavaScript，C++ 不命中 C#；`Java 或 Kotlin` 为 ANY，`Java 和 Spring` 为 ALL；原文只有 2 个可信 MUST 时只提出 2 个；完全没有可信核心组或当前版本未由业务人员核对时返回「资料需补全」。运行 `python -m pytest tests/match/test_core_skills.py -q`，确认失败。
- [ ] 复用已有 JD 解析结果，从结构化 `required_skills`、`requirements[kind=MUST]`、`core_duties` 和 JD 原文证据中选专业辨识词。按「明确 MUST → 岗位辨识度 → 职责证据」排序，去重取最多 5 个，不在匹配按钮中新增 LLM 提词调用；在当前 JD 版本记录业务人员核对后的组及核对状态，版本更新后状态失效。候选人的专业词从工作/项目技术栈和技能字段选最多 5 个，只用于召回。
- [ ] 用工作、项目、技能的原始内容验证 JD 核心组；画像总结、语义猜测和候选人关键词窗口之外的纯推断都不能放行。JD 其余显式硬性资质继续由 Task 2 检查。测试转为通过并回归技能边界测试。

## Task 4：职责和经历证据

**Files:** 新建 `match/duty_evidence.py`；修改 `search/contracts.py`、`search/lancedb_index.py`、`search/documents.py`、`search/sync.py`、`match/jd_index.py`；测试新建 `backend/tests/match/test_duty_evidence.py`。

**Interface:** `verify_duties(jd_duties: tuple[str, ...], resume_segments: tuple[EvidenceSegment, ...], *, threshold: float) -> DutyDecision`；`EvidenceSegment` 含原始文本、片段 ID、版本 ID、经历/项目来源；`DutyDecision` 含逐职责通过与来源。

- [ ] 写失败测试：仅简历画像相似、仅技术栈相同、仅行业相同不通过；具体工作/项目内容相符且能回指当前版本片段才通过；过期片段、缺原文、缺任一关键职责证据不通过。运行 `python -m pytest tests/match/test_duty_evidence.py -q`，确认失败。
- [ ] 在 `SearchChunk` 和 LanceDB 投影中增加行业、方向、片段来源字段，让两个方向都能在召回前按审核过的规范类缩小池。索引子片段明确区分 JD 职责与简历经历/项目，向量只对这些片段求相似；父文档或 AI 总结可帮助召回，但不作放行证据。修改片段格式时升级索引版本并重建受影响的向量，不混用旧向量。
- [ ] 在标注开发集上固定职责相似阈值与关键职责通过规则，选择满足「已知负例不进入」的保守值；独立集只验证。embedding 不可用或超时返回服务错误，不能用纯技能分自动放行。

## Task 5：两个按钮共用严格匹配与排序

**Files:** 修改 `match/service.py`、`api/match.py`、`runtime.py`；测试修改 `backend/tests/match/test_match.py`、`test_reverse_index.py`、`test_record_run.py`，新建 `backend/tests/api/test_strict_match_api.py`。

**Interface:** `match_jd(revision_id, limit) -> SearchPage` 与 `reverse_match_candidate(candidate_id, limit) -> list[ReverseMatchRecord]` 保留现有结果类型；额外附带行业、方向、技能组、职责证据和状态。API 只序列化通过准入的对象。

- [ ] 写失败 API 测试：两方向对同一人岗有相同通过/拒绝结论；先硬过滤再技能，再职责，再重排；返回全部合格项且 `limit` 生效；一人/岗位多 chunk 去重；没有合格项返回明确空；索引/embedding/rerank 不可用返回服务错误；旧版本/关闭岗位不可展示。运行 `python -m pytest tests/api/test_strict_match_api.py -q`，确认失败。
- [ ] 候选池可复用现有索引检索能力，但禁止把旧 keyword/vector/hybrid 任一模式的综合分直接当准入结果。JD→人先按行业/方向缩小人选池；人→JD 使用相同规则核对每个开放岗位，避免岗位索引未承载行业/方向时在召回前漏掉合格岗位。重排只处理已通过准入的集合；rerank 故障时失败关闭。
- [ ] 仅通过者落 `MatchRun/MatchResult`，保存证据和版本快照；既有旧运行记录标识旧算法，不与新结果混为当前推荐。运行匹配、API、索引状态和录入回归测试。

## Task 6：界面和验收替换

**Files:** 修改 `desktop/src/App.tsx`、`desktop/src/api/client.ts`、`desktop/src/pages/TalentPoolPage.tsx`、`desktop/src/pages/JdManagementPage.tsx`、相关前端测试及原十种方式验收文档。

- [ ] 先写前端失败测试：两个匹配入口不显示模式切换；列表只显示通过准入的人岗；可查看行业、方向、3～5 技能及职责片段证据；「资料需补全」「暂无符合要求」「服务不可用」三种提示不同。
- [ ] 客户端不再传 `mode`，JD 页移除匹配模式切换；人才库保留搜索模式切换，但候选人列「匹配岗位」按钮不使用搜索框所选模式。响应中将内部排序分称为「排序分」或隐藏，不写成百分比合适度。
- [ ] 运行前端单元测试和生产构建；在桌面实际检查两个按钮、空结果、资料缺失、故障和证据详情。更新评测合同，把双向六模式换成双入口零不合适准入验收，同时保持四种人才库搜索/筛选验收。

## Task 7（可选、最后实施）：手动 LLM 人岗分析

**Files:** 新建 `match/llm_analysis.py`；修改 `providers/ai/contracts.py`、`runtime.py`、`api/match.py`、`desktop/src/api/client.ts`、两页结果详情；测试新建 `backend/tests/match/test_llm_analysis.py`。

**Interface:** `POST /api/match/result/{result_id}/analysis` 返回持久 `task_id`；`GET /api/match/result/{result_id}/analysis` 返回 `未请求/排队/分析中/完成/失败` 和有证据引用的分析结果。

- [ ] 写失败测试：只接受当前有效且已通过准入的 `result_id`；未配置模型、超时、无依据内容、模型变化、JD/简历版本变化、重复点击、任务取消都不改变匹配列表或准入结论。运行 `python -m pytest tests/match/test_llm_analysis.py -q`，确认失败。
- [ ] 新增 AI `TaskKind.MATCH_ANALYSIS`，通过现有 `TaskRepository/TaskWorker` 排队。idempotency key 由候选人版本、JD 版本、规则版本、模型身份组成；结果以版本键缓存。生成任务只给模型当前原始证据，要求输出契合点、缺口、待核实问题和来源；禁止模型重新放行或添加候选对象。
- [ ] 页面在单条结果上提供「生成 AI 人岗分析」，点击后立即返回列表并轮询任务状态；失败可重试且规则结果仍可用。记录调用次数、耗时、输入量及费用代理；独立测 LLM 时间，不计入主匹配 P95。

## 最终验证与交付

- [ ] 在同一最终版本上跑开发集和未参与调参的独立集，两个入口返回项的硬条件违规数、明确不合适数、捏造关键证据数均为 0；报告漏掉合适项和原因，不因漏召回放宽准入。
- [ ] 运行后端相关回归、前端测试和生产构建，验证正常、缺字段、无结果、索引未就绪、embedding/rerank 故障及旧结果失效；量测完整 API P95 和模型请求数。
- [ ] 核对设计每项要求均有代码或测试覆盖，检查文档无占位内容，交付评测数据、失败样例及真实界面截图。可选 LLM 若未实施，明确标为未开启的第二阶段，不影响严格匹配主流程验收。
