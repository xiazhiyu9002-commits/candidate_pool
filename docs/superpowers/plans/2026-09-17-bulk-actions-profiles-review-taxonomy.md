# 批量操作、双形态画像与人岗复核 Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan task by task. Checkboxes track progress. This workspace has no `.git`; report changed files and test output at each checkpoint rather than inventing commits.

**Goal:** 在现有桌面人才库中完成三处列表批量操作、双向匹配复核展示、简历与 JD 画像的整体/分点双形态及分层索引，并以有证据的细分职业专长提高检索与匹配质量。

**Architecture:** 保留现有候选人、JD、流程与匹配实体。列表选择只传稳定实体 ID；批量操作复用单条业务服务，并报告逐项结果。画像在同一结构化生成结果中产出整体段落和对应分点，分别建立父/子向量；人工 JD 硬条件拆为可验证的词法/结构化约束。职业方向保留兼容主类，新增可多选的专业方向标签，避免新增混杂的硬筛主类。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy/SQLite、LanceDB、现有 embedding/重排提供者、React/TypeScript/Tauri、pytest、Vitest。

**Current-code basis:** 2026-09-17 源码。当前 `CandidateTable` 有单条下载、OCR、匹配、删除；`JdManagementPage` 与 `RecruitmentPage` 有单条删除。`match/review.py` 已有 `reasons/cautions` JSON 和项目优先的提示词，但反向匹配界面把它们压在一个单元格且未按复核结论重排。简历 `ai_profile_summary` 与 JD `candidate_profile` 的解析/回填提示词输出形态不一致。索引已有父/子片段。前轮准确度验收仍有纯向量和人工标签未达标项，不能把本计划的新向量结构直接视为改进。

## 设计决定与边界

1. 本计划把用户的“全占与 AI 开发”暂理解为“全栈与 AI 开发”。不新增一个将两种职责混为一类的顶层枚举。主方向继续为 BACKEND/FRONTEND/ALGORITHM/DATA/OPS/QA/PRODUCT/OTHER，`MANAGEMENT` 兼容旧值但优先作为属性；`specializations` 可同时包含 `FULL_STACK`、`AI_APPLICATION`、`DATA_PLATFORM`、`DATA_WAREHOUSE` 等。实施前用实际案例验证；若证据证明某类必须独立成顶层，再单独迁移。
2. 候选人批量选择按 `candidate_id`，JD 按 `jd_id`，流程按 `case_id`。表头“全选”只选当前显示页，跨页已选项保留并显示总数；修改搜索词、过滤条件或退出列表时清空。没有“选中全库 5 万人”。
3. 多选“匹配”是对每个已选候选人执行一次**人找岗位**基础匹配，汇总每人匹配的岗位数与结果入口；不自动执行可选 AI 深度复核。下载产出一个 ZIP；强制 OCR 入后台任务；删除永久生效，必须明确确认数量及 JD 删除会级联影响的流程数量。
4. 一份画像的整体与分点必须共享同一事实与人工覆盖。一次结构化生成先返回带证据路径的 `facts`，再返回基于这些事实写成的 `narrative`、`points`、`compact_context`；服务端检查每个关键事实同时出现在两种展示形态中，失败则标记待核且保留旧版。不能以两次独立生成互相猜测内容。简历侧和 JD 侧各有这两种形态，前端默认展示分点。现有 `ai_profile_summary` / `candidate_profile` 保持兼容，新增版本化字段，不丢手工编辑。
5. “卡 985”“字节或阿里背景”等显式硬条件从 JD 画像编辑文本中提取成 `exact_constraints`，在保存前把拟生效条目显示给用户检查和修改。学校档次和公司经历用结构化字段核验，词法索引保留同义词；`或` 为组内 OR，不把“优先”升级为 MUST。不确定的抽取保持待核并展示原句，不暗中硬拒候选人。语义向量仅承载业务、职责、项目和能力描述；人工硬条件与画像一同展示，但不重复灌进向量文本。
6. 整体画像是父切片；画像分点、每段工作经历、每个项目经历是同一父切片下不同类型的子切片，各自独立向量化、索引和召回。每个子切片的向量文本均为同一个短 `compact_context` 前缀加该子切片自身内容，并标记父 ID、实体/版本、片段类型与序号；前缀不能淹没项目或工作经历的独特内容。现有工作与项目经历不得被画像分点替代、合并或删除；JD 原有职责等证据片段也继续保留。向量方案要在隔离索引与固定盲评集上比较，才允许切换生产索引。

## 改动文件地图

| 单元 | 主要文件 | 职责 |
| --- | --- | --- |
| 批量选择/动作 | `desktop/src/components/CandidateTable.tsx`、`desktop/src/pages/TalentPoolPage.tsx`、`desktop/src/pages/JdManagementPage.tsx`、`desktop/src/pages/RecruitmentPage.tsx`、`desktop/src/App.tsx`、`desktop/src/api/client.ts` | 选择状态、工具栏、进度、结果 |
| 批量 API | `backend/src/kerui_recruit/api/resumes.py`、`backend/src/kerui_recruit/api/jd.py`、`backend/src/kerui_recruit/api/cases.py`、`backend/src/kerui_recruit/api/match.py`、`backend/src/kerui_recruit/runtime.py`，新建 `backend/src/kerui_recruit/bulk/service.py` | ID 校验、复用单条服务、ZIP、后台任务、逐项结果 |
| 复核 | `backend/src/kerui_recruit/match/review.py`、`desktop/src/pages/JdManagementPage.tsx`、`desktop/src/App.tsx`、`backend/src/kerui_recruit/api/match.py` | 项目证据、符合/注意点、反向排序与原件入口 |
| 双形态画像 | `backend/src/kerui_recruit/resumes/{profile,structured,normalize,pipeline}.py`、`backend/src/kerui_recruit/jd/{profile,structured,pipeline}.py`、`backend/src/kerui_recruit/providers/generation_tasks.py`、`backend/src/kerui_recruit/backfill/service.py` | 统一生成、兼容、人工覆盖与回填 |
| 词法硬条件与索引 | 新建 `backend/src/kerui_recruit/jd/profile_constraints.py`；修改 `backend/src/kerui_recruit/api/jd.py`、`backend/src/kerui_recruit/match/policy.py`、`backend/src/kerui_recruit/search/{documents,sync}.py` | 结构化 AND/OR、父/子向量、关键词检索 |
| 方向细化 | `backend/src/kerui_recruit/direction/{policy,classifier,backfill}.py`、`backend/src/kerui_recruit/search/direction.py`、`backend/src/kerui_recruit/match/service.py`、两个解析编辑器 | 专长标签、证据、兼容和回填 |

---

### Task 1：冻结新需求基线与行为契约

**Files:** Read `docs/verification/2026-09-17-search-match-v2-acceptance.md`; create `backend/tests/fixtures/profile_direction_v3.json`; write `docs/verification/2026-09-17-feature-baseline.md`.

- [ ] 先记录当前四个页面和两个画像的真实行为：单条操作、反向复核展示、画像手改与索引同步。冻结至少 10 个 JD→人、5 个人→JD、24 个搜索意图与 52 个方向边界卡片，补充各 10 个全栈/AI 应用边界样例。匿名、脱敏，保留人工与模型分歧，不把模型判分当真值。
- [ ] 记录 API 返回与旧 `parsed_data` 键名；写失败用例：JD 人工要求“卡985、字节或阿里”不能被纯向量相似度补偿；同一事实在整体/分点画像不能冲突；反向复核结果必须按推荐/复核/不推荐排序。
- [ ] 运行 `py -3.12 scripts/evaluate_semantic_quality_v3.py --baseline` 和既有聚焦测试，保存真实通过/失败数。前轮报告列出的未完成事项仍标“未完成”，不因本项目启动而消失。

**Gate 1:** 评测可重复，现有缺口和新需求有可检查样例。

### Task 2：先完成复核结果与详情的可见行为

**Files:** Modify `desktop/src/pages/JdManagementPage.tsx`, `desktop/src/App.tsx`, `backend/src/kerui_recruit/match/review.py`, `backend/src/kerui_recruit/api/match.py`; test `desktop/tests/App.test.tsx`, `backend/tests/match/test_review_service.py`.

- [ ] JD 找人的表头“推荐理由”改为“符合点”，保持 `reasons` 旧结果兼容，不改匹配得分。
- [ ] 人找岗位的 AI 复核分开显示两个列点区域：“符合点”和“注意点”。提示词必须比较 JD 的核心业务/项目要求与候选人的具体项目名称、业务场景、本人职责、技术和成果；各点附可追溯的项目/经历证据 ID，证据缺失时写“未见证据”，不编造经历。原始数据仍用 `reasons/cautions` 兼容旧 run，新增证据字段可选。
- [ ] 反向匹配新增独立“查看详情”列。因为每行 `revision_id` 是 **JD 修订 ID**，接口/抽屉状态另带这次匹配结果使用的 `resume_revision_id`；按钮调用已有 `viewResume` / `previewResume` 原件流程，不把 JD 修订误送简历预览，简历后来切换版本也仍能追溯当时证据。
- [ ] AI 复核完成后按 `recommend → pending（界面显示“复核”）→ reject` 排序，同类按基础匹配分降序、ID 稳定排序；AI 尚未完成或失败时沿用基础分排序。复核失败不清除原结果。前端断言列表分页按排序后的数组切片，而不是每页内局部排序。
- [ ] 运行 `py -3.12 -m pytest tests/match/test_review_service.py -q`（`backend`）及 `npm test`（`desktop`）。

**Gate 2:** 表头、两组要点、原始简历入口与排序在正反向真实样例可见；AI 仍为显式可选。

### Task 3：三个列表的多选与批量行为

**Files:** Modify `desktop/src/components/CandidateTable.tsx`, `desktop/src/pages/{TalentPoolPage,JdManagementPage,RecruitmentPage}.tsx`, `desktop/src/App.tsx`, `desktop/src/api/client.ts`; create `backend/src/kerui_recruit/bulk/service.py`; modify `backend/src/kerui_recruit/api/{resumes,jd,cases,match}.py`; tests `desktop/src/pages/TalentPoolPage.test.tsx`, `desktop/tests/App.test.tsx`, `backend/tests/api/test_bulk_actions.py`.

- [ ] 三处列表每行加复选框，表头全选当前显示页；工具栏显示已选数、取消选择、允许动作。人才库普通列表与搜索结果共用 `CandidateTable`，按 `candidate_id` 去重；筛选、搜索条件变化时清空，跨页保留已选数。
- [ ] 批量下载以选中候选人**最新有效原件**制作 ZIP，文件名冲突加匿名序号，漏文件在 ZIP 清单和结果摘要中列出；避免加载全部文件字节到浏览器内存。单条下载保持原行为。
- [ ] 修正当前“强制 OCR”单条请求实际发送 `force_ocr:false, use_vision:true` 的语义错误；批量动作明确发送 `force_ocr:true, use_vision:false`，让 OCR 路径真正执行。按选中版本入队，复用任务幂等键，展示成功入队/失败与任务进度，限制并发，不用一次请求同步等所有解析结束。
- [ ] 批量候选人匹配逐人执行现有 `matchCandidate` 对应的**人找 JD**服务，后台汇总每人 run ID、命中岗位数和失败原因；不自动 AI 深度复核。限制同时运行数量，关闭窗口后任务可查。
- [ ] 三类删除复用原 `CandidateDeletionService`、`JdDeletionService`、`case_service.delete`，批次上限和逐项成功/失败结果明确。确认框分别告知永久删除及关联影响，JD 额外统计并提示其流程会级联删除；失败项保持选中供重试，成功项从列表移除。
- [ ] 回归：跨页选择、当前页全选、重复 ID、已删除 ID、OCR 入队幂等、ZIP 重名/缺文件、部分失败、JD 级联影响、批量匹配不启动 AI。运行 `py -3.12 -m pytest tests/api/test_bulk_actions.py -q` 与前端聚焦测试。

**Gate 3:** 选择范围可见、每个结果可追溯，删除无静默扩散，OCR 确实走强制 OCR 路径。

### Task 4：画像双形态的数据契约与生成

**Files:** Modify `backend/src/kerui_recruit/resumes/{profile,structured,normalize,pipeline}.py`, `backend/src/kerui_recruit/jd/{profile,structured,pipeline}.py`, `backend/src/kerui_recruit/providers/generation_tasks.py`, `backend/src/kerui_recruit/backfill/service.py`; tests `backend/tests/api/test_profile_lifecycle.py`, `backend/tests/resumes/test_profile_pair.py`, `backend/tests/jd/test_profile_pair.py`.

**Contract:** 简历增加 `ai_profile_narrative: str`、`ai_profile_points: list[{text,evidence_paths}]`、`ai_profile_compact: str`；JD 增加 `candidate_profile_narrative`、`candidate_profile_points`、`candidate_profile_compact`。一次生成的内部草稿还包含 `facts: list[{text,evidence_paths}]`，用作一致性校验。旧 `ai_profile_summary` / `candidate_profile` 映射分点文本，供旧页面和旧索引版本兼容；`profile_version=3`、输入哈希、来源与过期标记一起更新。

- [ ] 先测试一个事实仅在整体出现/仅在分点出现时判生成结果无效；“参与”不能变“主导”；人工修改后重新解析/批量回填不覆盖；生成预览失败不清空已保存双形态。
- [ ] 简历与 JD 各用一次结构化模型输出共同事实、整体段落、3–6 个独立要点和 40–80 字整体浓缩上下文，按证据路径验证关键事实与数字；文本比对无法自动判定的情况标记待核，不猜测等义。不要分别调用两次自由文本模型来生成可能矛盾的两份画像。解析器中的旧单字段生成与专门画像生成器统一到同一契约，防止第一次解析和回填给出相反格式。
- [ ] 修改保存/重生成接口为双形态原子提交：预览只返回草稿，保存才同时写入两份、来源/hash/stale 并入队索引。人工只编辑分点时，从该分点内容产生等义整体草稿供一并保存；若转换失败，不能把旧整体内容留在新分点旁继续索引。前端默认显示和编辑分点，整体可查看但不占列表列宽。
- [ ] 旧记录继续显示旧画像；增量回填先转换并验证、跳过人工覆盖，失败不阻塞简历/JD 可用。报告需要生成/转换/人工跳过的数量与模型成本估算，不能直接对未来 5 万份无控制重生成。

**Gate 4:** 两种形态事实一致，手工修改不丢，旧资料可读，失败可恢复。

### Task 5：人工 JD 硬条件与词法/语义分流

**Files:** Create `backend/src/kerui_recruit/jd/profile_constraints.py`; modify `backend/src/kerui_recruit/api/jd.py`, `backend/src/kerui_recruit/jd/structured.py`, `backend/src/kerui_recruit/match/policy.py`, `backend/src/kerui_recruit/search/sync.py`, `desktop/src/components/JdProfileEditor.tsx`, `desktop/src/pages/JdManagementPage.tsx`; tests `backend/tests/jd/test_profile_constraints.py`, `backend/tests/match/test_match_policy.py`, `backend/tests/api/test_profile_lifecycle.py`.

**Contract:** `exact_constraints: list[{kind,operator,alternatives,strength,source,source_text}]`，`strength=MUST|PLUS|EXCLUDE`；组间 AND、组内 OR。`kind=school_level|company_history|skill|industry|other_keyword`，手工来源优先。学校/公司以结构化字段核验，`other_keyword` 需词法命中和活数据验证。

- [ ] 用“卡985；字节、阿里背景任选其一；LangGraph 优先”写失败回归：学校为 985 是 MUST；公司历史的字节 OR 阿里是另一个 MUST；LangGraph 是 PLUS，不能造成硬拒。测试同义公司名、缺证据、否定表达与用户清除约束。
- [ ] 手工画像保存先解析出约束草稿和剩余语义描述；编辑器显示 MUST/PLUS/EXCLUDE 可修改条目，原句可查看。明确的“卡985”可预选 MUST，含“优先”的条件预选 PLUS，含“或”的公司列为同一 OR 组；用户确认保存后才生效。解析信心不足时不自动把词设成硬条件；写入同一修订及 `manual_overrides`，触发词法索引同步。
- [ ] `evaluate_pair` 在双入口应用这些独立约束；命中词只能成为证据，不能用一个简历里“未达985”等否定句冒充学校资格。未满足 MUST 的配对不能被向量高分补偿；PLUS 仅改变排序。画像语义版本仍保留业务、项目与技术交付，硬条件不重复作为子向量。

**Gate 5:** 两个示例硬条件准确召回/排除，用户“优先”仍是加分，人工修改立即影响匹配而不污染语义向量。

### Task 6：父/子画像向量与隔离索引消融

**Files:** Modify `backend/src/kerui_recruit/search/{documents,sync,rebuild_maintenance}.py`, `backend/src/kerui_recruit/match/jd_index.py`; tests `backend/tests/search/{test_documents,test_sync,test_hybrid}.py`; create `scripts/evaluate_profile_chunks_v3.py`.

- [ ] 简历与 JD 的整体 `narrative` 各产生一个父向量。画像每个分点、每段工作经历、每个项目经历分别产生独立子向量，子片段文本为 `compact_context + 本片段内容`；子片段标记 `profile_point`、`work_experience`、`project` 类型，以及父 ID、序号和证据路径。JD 原有职责等证据片段也保留并关联 JD 父切片。词法索引单独保留公司、学校、技能及明确约束。不得用画像分点覆盖工作/项目片段，不把全技能表或短单词重复变成向量。
- [ ] 索引版本升级，在隔离副本比较旧索引、仅双形态、双形态加前缀三组。使用同一 24 搜索、10 JD→人、5 人→JD、手工补判的新前排结果，记录 P@5、已知相关对召回、明显误荐、片段数、embedding 数量与费用、索引体积、耗时。不能仅凭向量数量或结果更多判定获胜。
- [ ] 新旧版本可切换；先 dry run 和备份，再分批嵌入/重建，旧索引保留至准确度和性能达标。检查重建前后的工作经历、项目经历片段数及可召回样例；新画像片段增多时避免重复证据挤占候选人池。按现有样本每修订约 12.6 个片段，5 万份可能已达约 63 万片段；额外每份 3–6 个画像点会增加规模。用接近该片段数的隔离数据测首次搜索 P50/P95，不能只测 5000 份。

**Gate 6:** 整体画像父切片已向量化；画像分点、工作经历、项目经历各为独立子切片且均可单独召回；硬条件不进入语义片段。使用同一批人工复核标签重算旧索引基线和新索引，混合 P@5 不低于重算后的旧基线。旧报告的 0.8583 仅供定位，不拿模型标签分数直接与人工新标签比较。已知强相关漏检和误荐不回退；首次搜索目标 P50≤4 秒、P95≤8 秒，未达标不切换。

### Task 7：细分职业专长而不制造新的方向漏检

**Files:** Modify `backend/src/kerui_recruit/direction/{policy,classifier,backfill}.py`, `backend/src/kerui_recruit/providers/generation_tasks.py`, `backend/src/kerui_recruit/search/direction.py`, `backend/src/kerui_recruit/match/{policy,service}.py`, `desktop/src/components/{CandidateParsedEditor,JdParsedEditor}.tsx`, `desktop/src/pages/TalentPoolPage.tsx`; tests `backend/tests/direction/test_classifier_v3.py`, `backend/tests/match/test_match_direction.py`.

- [ ] 保留主方向/次方向/置信度/证据，增加 `specializations` 多值与 taxonomy v3。`FULL_STACK` 必须有前后端两侧的本人交付；仅技术列表同时含 React/Java 不够。`AI_APPLICATION` 需 Agent/RAG/模型 API 与业务系统集成的项目产出，模型训练、微调与算法优化仍属 ALGORITHM；数据平台管道与数仓建模按主要交付分开。
- [ ] 让 JD 与简历解析、确定性回退及人工编辑遵守同一判定表；对旧 MAIN 类做兼容映射，人工方向覆盖不被回填。用户手选专长过滤为精确条件；自动匹配将专长作为有证据的加权/救援信号，不能因标签缺失或旧 BACKEND 值而召回前拒绝。
- [ ] 52 个既有边界卡片加新全栈/AI 应用案例由模型与猎头视角分开复核。统计是否真的有职责独立、样本足够且造成检索错误的新顶层簇；满足才提出顶层类的独立迁移，不为 UI 名字强加枚举。

**Gate 7:** Agent 应用不误作训练算法，全栈需双侧职责证据，旧分类与人工覆盖可用，自动匹配无新硬筛漏检。

### Task 8：整体验收与交付

**Files:** Write `docs/verification/2026-09-17-bulk-profile-taxonomy-acceptance.md`; update `desktop/tests/App.test.tsx`, relevant backend suites.

- [ ] 后端运行 `py -3.12 -m pytest tests/api/test_bulk_actions.py tests/api/test_profile_lifecycle.py tests/match tests/direction tests/search tests/jd tests/resumes -q`（`backend`）；前端运行 `npm test`、`npm run build`（`desktop`）。将已有不相关失败与新回归分列，不能把未执行项写成通过。
- [ ] 验证单条操作未回归；所有批量动作对已选 ID 而非仅当前滚动行执行；删除确认影响范围准确；AI 复核默认关闭；画像手改与重新生成同步两种文本及索引；两入口同配对资格一致。
- [ ] 逐项写出 Gate 1–7 的证据、剩余风险和可复现命令。旧纯向量未达 P@5=0.60、人工方向真值未补齐等旧问题仍单列，除非新测试实证解决，不能借本项目宣布旧任务完成。

**完成标准:** 七项用户效果均在真实页面和固定业务样例可检查；批量操作的失败与影响清楚；每条画像的整体/分点事实一致且词法硬条件有效；项目经历主导 AI 复核要点；检索、双向匹配和 5 万份规模的性能没有明显回退。不要求企业级扩张，但不得留下已知强相关漏检或明显误荐而宣称完成。
