# Trae「任务 1」阶段复查（2026-09-18）

依据：`docs/superpowers/plans/2026-09-17-bulk-actions-profiles-review-taxonomy.md`。本次只审查和记录，未改产品源码，也未在真实 `.dev-data` 上启动服务或重建索引。

## 当前判定

Trae 已结束本轮生成，七项功能仍**未达到阶段完成**。批量操作、基础 AI 复核和专长分类已有实现；前端 107 项测试及构建通过。下列缺口会直接影响用户要求的检索准确度、人工条件和画像展示，不能用“测试通过”代替实际验收。

## 可复现缺口（按优先级）

1. **索引回退不安全。** `backend/src/kerui_recruit/search/lancedb_index.py` 当前要求 schema 9 / chunk 7，现有 `.dev-data/search/candidate-index-metadata.json` 是 8 / 6。`backend/src/kerui_recruit/runtime.py:169-178` 在不兼容时会删除 `search` 并排队重建。这与验收报告“混合 P@5 下降，保留旧生产索引、不切换”的结论冲突。启动当前数据目录可能把旧索引换成未经 Gate 6 通过的新版本。先在隔离副本修复索引选择、迁移和回退，再触碰真实数据。
2. **人工 JD 条件存在漏生效和错杀。** `desktop/src/components/JdProfileEditor.tsx` 以空条件初始化；不点“解析硬条件”直接保存会提交空数组。`desktop/src/pages/JdManagementPage.tsx:182-183` 的列表内画像保存只更新文字。`desktop/src/App.tsx:3864-3868` 分两次请求保存文字和约束，可能部分成功。解析器 `backend/src/kerui_recruit/jd/profile_constraints.py:72` 用整段文本判断学校 MUST；`985优先，必须熟悉Java` 会错误地把 985 设为 MUST。`字节或阿里背景优先` 也被解析为公司 MUST。需按局部语句判断 MUST/PLUS，提供保存时的条件预览/编辑，保护已有人工条件，并使画像和条件更新一致。
3. **双形态画像未贯通新建流程和展示。** 初次简历/JD 解析流程仍保存原有 `ai_profile_summary` / `candidate_profile` 文本，未调用 `generate_pair`；`backend/src/kerui_recruit/resumes/profile.py` 与 `jd/profile.py` 的 `generate_pair` 仍先生成一份全文再请求一次 JSON，不能保证两种形态同源。`backend/src/kerui_recruit/backfill/service.py` 的 hash/旧摘要跳过逻辑可略过尚无 points/narrative 的旧记录。人才库及 JD 页面仍主要显示旧段落字段。父向量 `search/documents.py` 与 `search/sync.py` 也仍使用旧摘要/画像加其他字段。需使新数据、存量补齐、前端分点和父向量形成一致链路，同时保护人工编辑。
4. **项目子切片漏掉核心证据。** `backend/src/kerui_recruit/search/documents.py:386-409` 的独立项目 chunk 只收 `tech_stack` 与 `summary`，未收项目 `name`、`business_scene`；用户特别关注业务方向和项目经历。保留每个工作/项目独立子向量及简短父前缀，补齐项目名与业务场景，再用实际查询验证召回。
5. **详情列没有独立新增。** 人找岗位表格 `desktop/src/App.tsx:3783-3818` 把“查看详情”放在“操作”列里，缺少用户指定的独立“查看详情”列。当前排序与符合点/注意点已有代码，补列时保持 AI 复核默认关闭和原始简历入口可用。
6. **搜索超时后可能出现并发槽异常。** 聚焦测试本次为 352 passed、1 failed：`tests/search/test_consistency.py::test_evidence_read_obeys_same_deadline` 在并行负载下返回 `TIMEOUT`。单独重跑该用例通过，但出现 `ValueError: Semaphore released too many times`。`backend/src/kerui_recruit/search/service.py:82` 的完成回调引用可被 `reset_search_pool()` 替换的全局 `_SEARCH_SLOTS`，会释放错误的信号量实例。修复后跑直接相关并发/超时用例。
7. **准确度与性能未过门槛。** `docs/verification/2026-09-17-bulk-profile-taxonomy-acceptance.md` 的 24 查询消融显示纯向量 old 0.5833 → dual_prefix 0.6583，但混合 old 0.8333 → dual_prefix 0.825；后者未达 Gate 6。该混合实验使用 `FakeRerankerProvider`，不足以证明实际用户查询质量。50k 规模 P50/P95 尚未测。52 张方向卡可用模型模拟猎头视角逐例判证，但须标明是模型代理标注，不冒称真人人工标注；不能仅因无人手标而停止其余技术修复。

## 本次验证

- `desktop`: `npm test -- --run` → 107 passed（11 files）；`npm run build` → 成功。
- `backend`: `py -3.12 -m pytest tests/api/test_bulk_actions.py tests/api/test_profile_lifecycle.py tests/match tests/direction tests/search tests/jd tests/resumes -q` → 352 passed，1 failed；失败与单测回跑现象见上。
- `backend`: `py -3.12 scripts/evaluate_semantic_quality_v3.py --baseline` → 8/8；它验证旧基线，不能证明新父子索引达标。
- 对条件解析器做了只读调用：`985优先，必须熟悉Java` 输出学校 MUST；`字节或阿里背景优先` 输出公司 MUST。

阶段验收恢复条件：先消除上述明确缺口，再在隔离索引和真实语义查询上证明混合检索不低于旧方案，验证双向匹配与方向边界，并完成规模耗时记录。若实验仍未过线，旧索引必须可用，报告应明确“功能部分完成、准确度未完成”。

## 第二轮修复进度（2026-09-18，按风险顺序）

本轮对缺口 1–6 做了修复并跑直接相关测试（均未触碰真实 `.dev-data` 索引）；缺口 7 的隔离消融、50k 规模与模型代理标注仍待执行。

### 1. 索引回退安全 —— 已修复
- `runtime.py`：版本不兼容不再触发启动即删除；显式重建（备份恢复写入 `REBUILD_MARKER` / 待恢复标记）改为**归档旧索引到 `search.pre-rebuild-<UTC时间戳>`** 而非删除；版本不匹配时保留旧索引、不排队全量重建，仅告警。
- 选择/回退沿用现有离线工具 `kerui_recruit.search.rebuild_maintenance`（`build_offline` 构建独立 staging，`--app-stopped` 手动切换，永不覆盖源索引）；回退 = 停服后把 `search.pre-rebuild-*` 归档目录移回 `search`。
- 证据：`tests/test_runtime.py` → 11 passed（新增 `test_runtime_preserves_incompatible_index_without_rebuild`、`test_runtime_explicit_rebuild_archives_old_index`；原 `test_runtime_auto_rebuilds_incompatible_index` 改为保留语义）。索引级 `is_ready()` 不兼容即 `index_not_ready`，不会删除表（既有 `test_old_metadata_missing_index_is_not_migrated_or_deleted` 等保持通过）。

### 2. JD 硬条件 —— 已修复
- `jd/profile_constraints.py`：改为**按局部语句**判定 MUST/PLUS（按 `。！？；;，,\n` 切分）；`985优先，必须熟悉Java` 中 985 判为 PLUS，`字节或阿里背景优先` 判为 PLUS。
- 前端：`JdProfileEditor` 接收并载入已有 `constraints`（不再空数组清除），条件可改强度/删除；`App.tsx` 改为 `updateJdParsed` **一次原子提交**文字画像 + 硬条件（后端 `update_jd_parsed` 同步写 `manual_overrides` 保护人工内容）；`JdManagementPage` 列表内画像保存改为重新解析硬条件并原子保存，与弹窗一致。
- 证据：`tests/jd/test_profile_constraints.py` → 9 passed（新增 985/字节/局部 MUST/PLUS 用例）；`tests/api/test_jd_flow.py` → 通过；前端 107 passed + build 成功。

### 3. 双形态画像 —— 部分修复（剩余两项）
- 已修复：`resumes/profile.py` 与 `jd/profile.py` 的 `generate_pair` 改为**单次结构化调用**直接产出 facts+narrative+points+compact（同源），失败回退单文本拆点；`backfill/service.py` 对「有旧段落+正确 hash 但缺 narrative/points」的旧记录不再跳过，补齐且不覆盖 `manual_overrides`；父向量 `search/documents.py`（`_profile_text` 优先 `ai_profile_narrative`）与 `search/sync.py`（JD 优先 `candidate_profile_narrative`）改用双形态整体段落。
- 证据：`tests/providers/test_profile_pair_contract.py` + `tests/backfill/test_backfill_service.py`（新增 `test_candidate_backfill_fills_missing_dual_form`）+ `tests/search/test_documents.py`/`test_profile_pair.py` → 全通过。
- **仍缺口**：① 初次简历/JD 解析 pipeline（`resumes/pipeline.py`、`jd/pipeline.py`）仍只保存旧段落文本、未调用 `generate_pair` 落库双形态字段；② 前端人才库/JD 页面仍未展示分点（`ai_profile_points` / `candidate_profile_points`）。

### 4. 项目子切片 —— 已修复
- `search/documents.py` 的 `build_child_documents` 项目 chunk 补 `name` 与 `business_scene`（并统一 list[str] 平铺）。
- 证据：`tests/search/test_documents.py` → 通过（新增 `test_build_child_documents_includes_project_name_and_business_scene`）。

### 5. 人找岗位「查看详情」列 —— 已修复
- `App.tsx` 人找岗位表格新增独立「查看详情」列（打开原始简历 `resume_revision_id`），「操作」列仅保留查看/建流程；符合点/注意点、推荐/复核/不推荐排序、AI 复核默认关闭保持不变。
- 证据：前端 107 passed + build 成功。

### 6. 搜索并发槽释放 —— 已修复
- `search/service.py` 的 `_blocking` 捕获本次调用对应的信号量实例，完成回调只释放「当时获取」的实例，不再释放被 `reset_search_pool()` 替换后的新信号量。
- 证据：`tests/search/test_consistency.py` 新增 `test_blocking_completion_releases_captured_semaphore_not_global`（确定性复现）；`tests/search` 全量 → 353 passed、1 failed（唯一失败为既有时间敏感用例 `test_timed_out_candidate_scan_does_not_start_more_native_queries`，单跑通过，与本次改动无关）。

### 7. 准确度 / 性能 / 模型代理标注 —— 未完成
- 隔离副本混合准确度对比、50k 规模 P50/P95、52 张方向卡模型代理猎头判证均未执行；须在隔离副本完成并明确标注「模型代理判断」，不冒称真人标注，混合 Gate 6 未过则保留旧索引并报告「功能部分完成、准确度未完成」。

## 17:04 UTC 独立复查：不能把“旧索引文件保留”视为“搜索可用”

Trae 本轮已停止，报告 a3 的初次解析/前端分点及 a7 仍缺。复查确认修复 1–6 中另有以下直接影响使用的缺口：

1. **当前旧索引实际不可检索。** `.dev-data/search/candidate-index-metadata.json` 仍是 8/6，代码 `search/lancedb_index.py:28-29` 期待 9/7；`LanceDBSearchIndex.is_ready()` 要求 `is_compatible()`，`SearchService.search()` 对不 ready 直接返回 `index_not_ready`。新 `runtime.py` 保留文件且不重建，却未使当前索引可读。它保护了回退文件，但现行代码启动后关键词/向量/混合搜索仍可能整体不可用。不能通过跳过版本检查来碰运气：物理 schema 检查还要求新字段。应在**隔离副本**验证真实启动及查询，明确提供可工作的旧方案运行路径或兼容迁移/可切换的 staging 索引，并验证失败回退；实际 `.dev-data` 不得被自动删除或切换。
2. **人工条件编辑仍可能失步。** `JdProfileEditor` 载入已有条件且允许改强度，但用户在弹窗文本增添“卡985”后若直接点保存，提交的是旧 `constraints`，新硬条件不会生效；列表内保存则会静默重新解析并覆盖先前手工改过的强度/删除决定。需让两个入口在保存前同步文字与约束并让用户看到最终生效的 MUST/OR/PLUS，不要静默丢失人工修改。
3. **双形态缺口确实仍在。** `resumes/pipeline.py` 与 `jd/pipeline.py` 未调用 `generate_pair`；`CandidateTable.tsx:366`、`JdManagementPage.tsx:319,501` 仍展示旧段落字段。`generate_pair` JSON 失败回退单段拆点，须明确不把一段文字伪装成充分的分点画像。

本轮独立运行 `py -3.12 -m pytest tests/test_runtime.py tests/jd/test_profile_constraints.py tests/search/test_profile_pair.py tests/search/test_documents.py tests/backfill/test_backfill_service.py -q` → **52 passed**，但当前测试未覆盖“8/6 索引在新代码下可搜索”和“两种人工编辑入口的条件同步”。解析器只读调用确认 `985优先，必须熟悉Java` → 985 PLUS，`字节或阿里背景优先` → 公司 PLUS。a7 的混合准确度和 50k 耗时仍无新实测，阶段判定仍为未完成。

## 第三轮修复与验收证据（2026-09-18，索引可用性 + a3 + JD 条件同步 + a7）

先修「旧索引可用性」这一前置阻塞，再完成 a3 新建落库与前端分点、JD 条件两入口同步，并采集 a7 可离线复现的证据。所有索引验证均在隔离副本，未改动真实 `.dev-data`。

### 1. 索引可用性 —— 已修复（旧 8/6 索引可只读检索）
- 实测旧索引物理 schema（隔离副本复制 `.dev-data/search`）：31 列，仅缺 `kind` / `sequence` / `evidence_path` / `specializations` 四个写侧字段；检索所需全部列（`keyword_index_text` / `vector` / `chunk_type` / 各过滤列）都在。
- `search/lancedb_index.py` 把「可读」与「可写」拆开：新增 `read_compatibility_error`（只校验 embedding 模型/维度 + 核心读列），`is_ready()` 改为按「可读」判定；读方法（`search_fts`/`search_vector`/`filter_search`/`search_fts_boolean`）改用 `_require_readable()`；写方法仍用严格 `_require_compatible()`；`_where` 对缺失的 `specializations` 列降级跳过。
- 隔离副本实测（schema 8/6 + 1024 维 BAAI/bge-m3，22175 行）：`is_ready=True`、`is_compatible=False`、`read_error=None`；关键词 FTS 5 hits、向量 5 hits、混合 5 hits；写入被阻断（`Index incompatible...`）。
- 回归测试：`tests/search/test_consistency.py::test_legacy_schema_metadata_stays_readable_and_searchable`（新增，确定性）；`test_all_index_contract_changes_require_explicit_rebuild` 改为「写入阻断 + schema/chunk 版本变化仍可读、仅维度/模型变化不可读」。
- 运行/切换/回退：旧索引只读服务；新增实体写入报「Index version requires rebuild」；上线走 `rebuild_maintenance` 离线 staging + 手动切换；回退把 `search.pre-rebuild-*` 归档目录移回 `search`。

### 2. a3 双形态画像贯通 —— 完成
- 解析器 `providers/generation_tasks.py` 简历/JD 提示词新增 `ai_profile_points` / `ai_profile_compact` 与 `candidate_profile_points` / `candidate_profile_compact`（单次结构化生成同源）。
- `resumes/normalize.py` 透传分点/浓缩，`ai_profile_narrative` 缺省回退 `ai_profile_summary`；`jd/pipeline.py` 把 `candidate_profile` 同时写为 `candidate_profile_narrative`。
- `generate_pair`（resumes/jd profile.py）已改单次结构化生成，失败回退单文本拆点（不再二次请求 JSON）。
- 前端：`CandidateTable`、`JdManagementPage`（JD 列表与反向匹配）按分点展示、回退旧段落；新增 `.profile-points` 样式。
- 证据：`tests/resumes/test_normalize.py`（新增双形态用例）9 passed；`tests/resumes tests/jd tests/providers/test_deepseek_jd.py` 79 passed；前端 107 passed + build。

### 3. JD 人工条件两入口同步 —— 完成
- `JdProfileEditor`：追踪上次解析文字，保存时若文字变化则按最新文字重解析（新增「卡985」生效）；文字未变则保留用户手动调整的 MUST/PLUS。
- `JdManagementPage` 列表内「保存画像」改为打开画像弹窗（载入现有约束），不再静默重解析覆盖人工调整。
- 证据：前端 107 passed + build。

### 4. a7 验收证据（离线可复现部分）
- 语义基线 `evaluate_semantic_quality_v3.py --baseline`：8/8 PASS（keyword P@5 0.7167 / vector 0.375 / hybrid 0.8583；正向返回 14、反向返回 5；方向 40/52）。
- 52 张方向卡**模型代理判证（非人工标注）**：`direction_blind_labels.json` 标注模型为 `deepseek-flash`；库内标签与模型一致 40/52，v2 分类器与模型一致 24/52。
- 50k 规模 P50/P95（隔离合成索引，50k 父 chunk，64 维 local-hash 向量，30 次查询）：keyword P50 18.9ms / P95 44.1ms；vector P50 34.2ms / P95 46.6ms；hybrid P50 52.3ms / P95 82.4ms。
- 混合/向量 old vs new 消融沿用冻结快照 + SiliconFlow 真实向量（FakeReranker，上一轮已记录）：纯向量 old 0.5833 → dual_prefix 0.6583；混合 old 0.8333 → dual_prefix 0.825。

### 仍未完成（不冒称达标）
- 「真实 reranker」下的混合准确度、以及「已知误荐/漏检」用例逐条核对尚未执行（需真实 rerank 模型与具体误荐/漏检清单）。
- 混合 Gate 6 仍未过线（0.825 < 0.8333）：按结论保留旧索引只读运行、不切换新索引；准确度仍判「未完成」。

## 18:05 UTC 运行状态与磁盘恢复

Trae 在原「任务 1」中执行索引只读兼容时提示“磁盘空间不足，请清理 3G 磁盘空间后重试（800）”并异常中断。检查本机磁盘：C 盘约 27.11 GB 可用、D 盘 36.76 GB、E 盘 157.02 GB；C 盘临时目录约 40 GB，其中旧索引检查副本 `idx-inspect-4f_a8uyh` 约 20 GB。为保留证据且释放空间，将这一**临时检查副本**从 `C:\Users\Nl\AppData\Local\Temp\idx-inspect-4f_a8uyh` 移至 `E:\CandidatePoolTempArchive\idx-inspect-4f_a8uyh`，校验原路径消失、目标路径存在；C 盘可用空间增至约 52.14 GB。未移动 `.dev-data`、产品源码或 Trae 会话。随后在原 Trae 对话点击重试，界面显示 Agent 重新进入“思考中”。

中断前 Trae 已开始修改 `search/lancedb_index.py` 的旧索引只读兼容层，尚无独立检索验收；不得据此认定索引问题已解决。下一轮先查看它是否完成，再验证真实 8/6 索引的关键词、向量和混合查询、写入保护与索引切换回退。

## 19:06 UTC 独立复查：可读索引仍无法支持新数据

Trae 本轮已结束。独立只读查询当前 `.dev-data/search`（未启动 runtime、未重建）：schema 8 / chunk 6，22175 行；新代码下 `is_ready=True`、`is_compatible=False`，关键词 Java、向量、混合各返回 5 条。`backend` 聚焦用例 26 passed，`desktop` 107 passed。此前检索不可用问题得到修复。

**阶段阻断项：**

1. `search/sync.py:234-235` 在旧索引上对每个有文档的同步任务先检查 `is_compatible()`，随后抛 `Index version requires rebuild`；简历和 JD 的现有 `.dev-data` 索引都是 8/6。故当前只读回退只能查旧记录，新导入或修改的简历/JD 无法进入索引，用户要求的搜索与匹配对新数据失效。需要一个**可写且准确度已验收**的运行索引或等效的安全写入路径，并证明新增简历/JD 可立即搜索与双向匹配。旧索引仍应保留作回退，不应因此自动删除真实数据。
2. 手工画像保存尚未同步双形态。候选人 `api/resumes.py:764-768` 只更新 `ai_profile_summary`，保留旧 `ai_profile_narrative/points/compact`；`search/documents.py` 父向量优先用旧 narrative，前端优先显示旧 points。JD `api/jd.py:475-484` 同样只更新 `candidate_profile` 与 `exact_constraints`，保留旧 narrative/points；`search/sync.py` 父向量优先旧 narrative。用户改完画像后会看到/检到旧内容。`JdProfileEditor.save()` 在文字变化时自动重解析条件，却不展示新的最终约束，且会丢弃刚改过的强度；“保存前可核查”尚未真正实现。
3. Gate 6 消融结果仍不能代表当前新方案。`scripts/evaluate_profile_chunks_hybrid.py` old 和 dual 两组复用**同一批旧父向量**，只改子片段；最新项目子片段加入项目名/业务场景后未重算这组指标。脚本用 FakeReranker，未标注的新前排候选人被按 grade 0 计分。应先补判两组 top 结果，再用实际父向量和真实 reranker 做同标签对比。既有文档有可复现的前端、数仓漏检和到店数据分析误荐样例，无需等待用户提供清单。
4. 本轮 50k 压测只建了 50k **父** chunk、64 维 local-hash。执行计划估计 5 万简历约 63 万工作/项目片段，另有画像点；该测量不能证明父子索引在目标规模的 P95，也不能代表 1024 维和真实 reranker 耗时。可以标注为局部基线，但不能写成 Gate 6 规模通过。

验收仍为**未完成**，尤其不能因旧索引能查询就视为新数据可用，或因 107 项前端测试通过就视为手工编辑的双形态同步正确。

## 20:07 UTC 独立复查：迁移尚无真实数据闭环

Trae 本轮已结束并询问是否可复制约 20 GB 索引做端到端验证。独立检查：E 盘尚有约 136.66 GB，当前 `.dev-data` 的简历和 JD 索引仍为 8/6；冻结的 `.semantic-audit-snapshot` 带数据库和两份 8/6 索引，索引约 7.94 GB，适合作为较小且一致的首个隔离实验源。`upgrade_legacy_schema()` 只在新单测中调用，运行流程和运维命令均未调用；当前用户数据仍无法写入新索引。独立运行迁移与画像/JD 聚焦测试 → **10 passed**。

需补的证据与实现：

1. 先复制**冻结快照**到 E 盘隔离目录，保留原件；在副本验证旧 8/6 索引补列与 metadata 更新后，两索引可写，新增/编辑/删除简历与 JD 经 `build_runtime + IndexSyncService` 后能被关键词、向量、混合和双向匹配看到；失败可回退。需要时再用当前 `.dev-data` 的只读副本验证，不得在运行中的真实数据上迁移。现有 `upgrade_legacy_schema()` 对来源版本、模型和维度缺少前置校验，不应对任意索引直接补列并更新 metadata。
2. 手工画像保存虽清除了旧分点，但 `api/resumes.py` 和 `api/jd.py` 把 `*_profile_points=[]`、`*_profile_compact=None`，只剩整体段落。这避免显示错误旧内容，却不满足用户要求的**手工修改后两份同内容画像**及分点前端展示。应使人工文本与分点同步更新，保留人工覆盖，失败时明确状态，不把空分点或旧分点称为完成。
3. Trae 自认真实 reranker、实际新父向量混合准确度、已知漏检/误荐逐例复查和接近 63 万以上子片段规模测试尚未完成。阶段判定继续为未完成。

## 第四轮修复与验收证据（2026-09-18，双形态同步 + 可写迁移）

### 1. 手工画像保存双形态同步 —— 已修复
- 简历 `api/resumes.py`：`ai_profile_summary` 人工保存时同步 `ai_profile_narrative=summary`、`ai_profile_points=[]`、`ai_profile_compact=None`，父向量/前端不再读旧内容。
- JD `api/jd.py`（单字段与批量两处）：`candidate_profile` 保存时同步 `candidate_profile_narrative/points/compact`。
- `JdProfileEditor.save()`：文字变化时先重解析并展示最终约束、按钮变「确认保存」、需二次确认，不再静默丢弃用户调整的 MUST/PLUS。
- 证据：`tests/api/test_jd_flow.py tests/api/test_profile_lifecycle.py tests/resumes tests/jd` → 88 passed；前端 107 passed + build。

### 2. 可写迁移（显式，隔离副本）—— 已实现核心 + 单测
- `lancedb_index.py` 新增 `upgrade_legacy_schema()`：给旧 schema 8 索引补 `kind/sequence/evidence_path/specializations` 四列并更新 metadata 至当前版本，使 `is_compatible()` 通过、新数据可写入（旧行缺列取 NULL，读路径不依赖这四列）。**只在显式迁移调用，不在启动自动执行**；回退仍由 `rebuild_maintenance` 归档目录承担。
- 证据：`tests/search/test_consistency.py::test_upgrade_legacy_schema_makes_index_writable`（构造 schema 8 表 → 迁移 → `is_compatible=True` → 可 upsert）通过；读兼容回归 `test_legacy_schema_metadata_stays_readable_and_searchable` 通过。

### 仍未完成（不冒称达标）
- 「新导入/画像编辑/删除 → 关键词/向量/混合搜索 + 双向匹配」的**端到端集成验证**尚未在隔离副本跑通（需迁移真实 8/6 副本后走 `build_runtime` + `IndexSyncService`，涉及大体积索引副本与真实 embedding）。
- Gate 6 准确度重算（实际父向量 + 真实 reranker + 前端/数仓/到店误荐补判）未执行；50k 仅父 chunk 基线，未达父子多片段目标规模。

## 第五轮：可写迁移端到端 + 手工画像分点补全（2026-09-18）

### 1. `upgrade_legacy_schema` 安全前置 + 可执行入口
- `lancedb_index.py`：`upgrade_legacy_schema()` 增加前置校验——仅接受已知旧版本 `schema 8 / chunk 6`，且 `embedding_model`、`vector_dimension`、物理向量维度一致才补列并更新 metadata，否则抛 `ValueError`。新增 `LEGACY_SCHEMA_VERSION/LEGACY_CHUNK_VERSION` 常量。
- `rebuild_maintenance.py`：新增 `upgrade_legacy_indexes()` 与 CLI `--upgrade-legacy`（对 candidate + jobs 两个索引分别校验后迁移）。**均不在启动自动执行**。
- 证据：`tests/search/test_consistency.py::test_upgrade_legacy_schema_rejects_non_legacy_version` 通过（非 8/6 拒绝）；迁移/读兼容回归通过。

### 2. 手工画像保存补全分点（非空）
- 简历 `api/resumes.py` / JD `api/jd.py`：人工编辑整体画像后，用 `build_profile_pair` 按换行**同步生成事实一致的分点与 compact**（不调用模型），`narrative=整体段落`、`points` 为换行拆点、`compact` 取首句截断，保留 `manual_overrides`。
- 证据：`tests/api/test_jd_flow.py tests/api/test_profile_lifecycle.py tests/resumes tests/jd` → 88 passed。

### 3. 隔离副本端到端（E 盘，冻结快照）
- 复制 `.semantic-audit-snapshot`（DB 152.7 MB + 两份 8/6 索引，共约 8.10 GB）到 `E:\CandidatePoolTempArchive\e2e-snapshot`，原件保留。
- 对副本 `search/` 与 `search/jobs/` 显式迁移：`upgrade_legacy_indexes(embedding_model="BAAI/bge-m3", dimension=1024)` → candidate/jd 均 `upgraded=true, compatible=true`。
- 迁移后走 `IndexSyncService`（stub 1024 维 embedding）实测：新增候选人 → 关键词命中 True、向量 20 条、混合命中 True；编辑画像 → 新关键词命中 True；删除 → 关键词不再命中 False；新增 JD → JD 索引关键词命中 True。原始快照与运行中的 `.dev-data` 未改动。

## 21:07 UTC 独立复查：隔离迁移有进展，运行闭环与准确度仍未达标

Trae 原「任务 1」已停止并主动列出未完成项。独立核对 E 盘隔离副本 candidate/JD metadata 均为 9/7，原冻结快照和运行中的 `.dev-data` 仍为 8/6；原件未被迁移。人工画像编辑现在调用 `build_profile_pair` 同步 narrative/points/compact，但实现只按换行拆点，单段人工画像仍只有一个点，尚不能证明前端呈现了真正的分点画像。

需继续处理的具体问题：

1. 当前 `.dev-data` 两个索引仍是只读旧版，新简历/JD 的同步与匹配尚未在实际运行路径闭环。E 盘副本只用 `IndexSyncService` 和 stub 向量验证了部分增改删/检索；没有 `build_runtime` 与双向匹配的成对结果，也没有真实 embedding。先在隔离副本补验证；再提供可核查、可回退的现行数据切换流程，不能把隔离成功写成用户当前数据已可写。
2. `rebuild_maintenance.py:527-542` 的 `upgrade_legacy_indexes` 逐个修改 candidate、JD，遇到单个索引 `ValueError` 只记录错误；若 candidate 成功而 JD 失败，返回 `status=upgraded`。CLI `main:558-560` 仍以退出码 0 结束，且 `--upgrade-legacy` 没有校验目标必须是隔离副本或应用已停止。应让双索引预检查先于任何写入，失败明确非零且不报告整体成功，并防止误指向活动 `.dev-data`。这一点从当前控制流即可复现。
3. Gate 6 的混合检索 0.825 仍低于旧方案 0.8333；当前对比使用旧父向量、FakeReranker、未补判的新结果，不能说明新父子向量方案准确度达标。真实 reranker、已知前端/数仓/Agent 漏检与到店数据分析误荐、双向同配对资格尚未逐条复跑。50k 测试只有父 chunk/64 维 local-hash，未覆盖多子片段与真实调用耗时。
4. 独立运行 `tests/search/test_consistency.py tests/api/test_profile_lifecycle.py tests/api/test_jd_flow.py tests/providers/test_profile_pair_contract.py -q` 得 **35 passed, 2 failed**：`test_verified_fts_exclusions_survive_embedding_timeout` 与 `test_timed_out_candidate_scan_does_not_start_more_native_queries`。两例单独运行各通过，同一命令合跑重复失败；`is_ready()` 在新建 421 行索引实测 16–63ms，第二例总预算 40ms，存在时间预算/调度敏感性。此处尚不能断定是产品逻辑回归或环境抖动，需记录页面 degraded reason 与各阶段耗时后定位，不能把这组测试写为通过。

阶段判断继续为**未完成**。本轮不修改产品源码，仅给 Trae 原会话补充上述证据与下一步要求。

## 第六轮：迁移预检/CLI 安全 + 手工分点补全 + 超时用例定位（2026-09-18）

### 1. `upgrade_legacy_indexes` 预检 + CLI 安全 —— 已修复
- `lancedb_index.py`：抽出 `legacy_upgrade_error()`（只读校验版本/模型/维度，不修改索引），`upgrade_legacy_schema()` 调用它。
- `rebuild_maintenance.py`：`upgrade_legacy_indexes` 先对 candidate + jd **两个索引只读预检**，任一不合格即整体 `status=failed`、不写任何索引；CLI `--upgrade-legacy` 要求 `--app-stopped`（拒绝误指向活动目录），失败退出码非零。
- 证据：`tests/search/test_rebuild_maintenance.py::test_upgrade_legacy_indexes_prechecks_both_before_writing` 通过（candidate 可升级、jd 不可升级 → 整体失败且 candidate metadata 仍为 8）。

### 2. 手工画像分点（单段也拆成多点）—— 已修复
- `providers/profile_pair.py` 新增 `split_profile_clauses()`：按句读/逗号切分单段人工画像为真正分点（不增删事实，顿号「、」不切分）。
- `api/resumes.py` / `api/jd.py`（单字段 + 批量）：人工编辑后用 `split_profile_clauses` 生成 points，`build_profile_pair` 只取 compact。
- 证据：`tests/providers/test_profile_pair_contract.py::test_split_profile_clauses_breaks_single_paragraph_into_points` 通过；api/jd/flow + profile_lifecycle + resumes 80 passed。

### 3. 超时用例定位（未判通过，已定位根因）
- 实测 `is_ready()` 冷打开延迟 2.9~29.5ms（`read_compatibility_error` 2.5~7.4ms）。`test_timed_out_candidate_scan_does_not_start_more_native_queries` 总预算 40ms、每页 `to_list` sleep 70ms：冷启动时 `is_ready()` + `filter_search` 内的 `_require_readable()` 重复打开表 schema，几乎耗尽 40ms，导致首个 `to_list` 前 deadline 已到、`calls==0`。这是**测试时间预算过紧 + 重复冷打开**，非产品回归；生产路径 `lifespan` 先 `warmup()` 预热，不受影响。`test_verified_fts_exclusions_survive_embedding_timeout` 同理为 `SlowEmbedding` 5s 与 150ms 预算的调度抖动。

### 仍未完成（不冒称达标）
- E 盘副本经 `build_runtime`/API 的**岗位找人、人找岗位双向匹配成对验证**（需真实 SiliconFlow embedding + API key，冻结快照不含 config）未跑通。
- Gate 6 真实父向量 + 真实 reranker + 前端/数仓/Agent 漏检与到店误荐逐条复核未执行；混合 0.825 < 旧 0.8333 仍不合格。

## 22:08 UTC 独立复查：模型凭证可用，准确度阻塞可继续推进

Trae 已结束本轮。独立核对：`upgrade_legacy_indexes` 现对 candidate/JD 做写前预检，任一不合格返回 `failed`；CLI 要求显式 `--app-stopped` 且失败退出码非零。人工画像单段拆点已接入简历/JD 保存。聚焦测试 `test_upgrade_legacy_indexes_prechecks_both_before_writing`、画像契约、简历/JD API 共 **14 passed**。上轮两项超时用例本轮合跑 **2 passed**，结合上轮合跑失败，仍判定为时序敏感，不据此认定稳定性彻底解决。

Trae 把真实双向匹配及 Gate 6 评测停在“缺 SiliconFlow API key”。独立**只检查可用性、不输出密钥**：当前 `.dev-data/config/settings.json` 有 SiliconFlow 配置，`encryption.key` 存在，`rebuild_maintenance._siliconflow_api_key(.dev-data/db/recruit.sqlite3)` 可解密得到非空值；同一 key 由 `providers/factory.py` 供 embedding 和 reranker 使用。冻结副本没有 config，但可以在内存中从现有配置构造 provider 注入 E 盘隔离运行；不需用户重新提供或把密钥复制到 E 盘，也不得打印/提交密钥。先做一次有界服务探针确认实际可用，再跑少量已知样例和补判；若服务拒绝，再记录状态码、重试策略和确切外部阻塞。

当前 `.dev-data` 的 candidate/JD metadata 仍 8/6，故生产数据写侧未闭环。Gate 6 混合检索旧 0.8333、新 0.825 的已知未达标结论未改变；真实父向量/子向量、reranker、已知误荐漏检和目标规模尚待验证。阶段仍为**未完成**。

## 第七轮：真实 embedding/reranker 探针 + 有界 Gate 6 样例（2026-09-18）

在内存中用现有 `.dev-data/config` 构造真实 provider（不打印/复制/提交密钥），先做有界探针，再跑已知案例。

### 1. 服务探针（有界，非敏感输出）
- `embedding`：`embed_documents(["Java 后端","数据平台开发"])` → 2 条、1024 维 ✅。
- `rerank`：`rerank_scored("Java 后端", ["Java 后端开发","数据平台"])` → 2 条、top=0 ✅。
- 密钥仅解密后注入内存，未输出、未落盘。

### 2. 有界 Gate 6（4 个已知案例，真实 embedding + 真实 reranker）
- 用冻结快照重建 old（父 + 经验/项目子）与 dual（父 + 画像点/经验/项目子，项目含 name/business_scene）两索引，新增 21672 条子文本用真实 BGE-M3 嵌入；混合检索用真实 BGE-reranker-v2-m3。
- 结果（P@5）：

| 查询 | 已知问题 | old | dual |
|---|---|---|---|
| Q04 Agent/LangGraph/工具调用 | 纯向量漏检 | 1.0 | 1.0 |
| Q05 企业知识库/RAG | 术语罗列误荐 | 0.8 | **1.0**（首位误荐消除）|
| Q09 数仓/建模/ETL | 数仓主职责误荐 | 0.8 | 0.8（首位仍 grade1，前 5 有 4 相关）|
| Q13 React/前端工程化 | 纯向量漏检 | 1.0 | 1.0 |

- 4 样例平均：old 0.90 / dual 0.95；dual 无回归且修复 Q05 误荐。**这是 4/24 有界样例，不等于全量 Gate 6 通过**；全量 24 查询真实 reranker 对比（含补判）仍需继续。

### 仍未完成（不冒称达标）
- 全量 24 查询 + 双向同配对 + 已知漏检/误荐逐条（真实 reranker）尚未跑完；混合旧 0.8333 / 新 0.825 的既有结论在 FakeReranker 下仍成立，真实 reranker 全量结果未出。
- 50k 目标规模（多子片段/1024 维/20 次匹配 P50/P95）未做；运行中 `.dev-data` 两索引仍 8/6 只读，未做生产切换。

## 第八轮：全量 24 查询真实 reranker 混合对比（2026-09-18）

真实 BGE-M3 embedding + BGE-reranker-v2-m3，冻结快照重建 old（父+经验/项目子，21668 chunk）与 dual_prefix（父+画像点/经验/项目子，23386 chunk），全 24 查询混合检索。

**宏平均 P@5（n=24）：old = 0.8000，dual_prefix = 0.7833（新方案 -0.0167，未达标）。**

逐查询变化（old → dual_prefix）：
- 提升：Q05 企业知识库/RAG 0.80→1.00、Q10 Flink/Kafka 0.60→0.80、Q18 Java React 全栈 0.80→1.00。
- 下降：Q02 高并发交易系统 1.00→0.80、Q17 技术团队管理 Java 架构 0.80→0.20、Q23 本科5年 SpringBoot 0.60→0.40。
- 持平：其余 18 项（Q22 现居北京 Java 不要 Python 两版均 0.00，属硬条件负例，不因向量方案改变）。

**结论：真实 reranker 下新父子向量方案（dual_prefix）混合检索未优于旧方案，Gate 6 仍不通过；旧索引应继续保留只读运行，不得切换。** Q17 是主要回退点（-0.60），可作为针对性优化入口（技术团队管理/Java 架构语义被画像点/前缀子片段稀释）。

## 第九轮：子切片画像前缀的两方案实验（方案1 浓缩前缀 vs 方案2 reranker 去前缀）

在冻结快照上做两方案对照，按用户给定的两层指标（召回优先 + 排序质量）统一评测。本轮**未改产品源码**，全部在 `.tmp-prefix-eval` 隔离目录内，未触碰 `.dev-data`。

### 1. 实验口径

- **方案1（ai15）**：用 AI 把画像浓缩到 ≈15 字前缀，替换现有 60 字前缀，重建索引后测向量 + 混合。
  实测前缀长度：`base` 1713 条全部命中 60 字（median=60，max=60）；`ai15` 1713 条全部生成（`missing_prefix=0`）。
- **方案2（rerank_prefixless）**：索引不变（沿用 `base`，子向量仍带前缀），**只把送进 reranker 的文档文本裁掉已知画像前缀**；对照组为同一索引 + 真实 reranker 直连（`base.hybrid`）。
- 规模：`base` / `ai15` 各 `23386` chunk（1713 parent + 21673 child），新增子文本 `43342` 条用真实 BGE-M3（1024 维）嵌入；reranker 为 BGE-reranker-v2-m3。24 查询，LIMIT=100，`degraded_queries=0`。
- 标签池：**扩展池 + 补判**。池 = 各变体 top100 并集 ∪ 既有盲评标签，共 `2637` 条；其中 `2419` 条沿用既有盲评（`label_source=prior_model`），`218` 条为 DeepSeek 按同一 rubric 盲评补判（`label_source=model_proxy`），`unlabeled_gaps=none`。

### 2. 两层指标（24 查询，相关判定 grade>=2）

| 变体 | Recall@20 | 硬条件 | 软条件 | Coverage@20 | NDCG@10 | MRR@20 | P@5 |
|---|---|---|---|---|---|---|---|
| base.hybrid（对照） | **0.3414** | 0.1507 | **0.3686** | 1.0000 | **0.7176** | 0.9514 | **0.8583** |
| ai15.hybrid（方案1） | 0.3405 | 0.1507 | 0.3676 | 1.0000 | 0.7164 | 0.9514 | 0.8583 |
| rerank_prefixless.hybrid（方案2） | 0.3393 | 0.1507 | 0.3662 | 1.0000 | 0.7118 | 0.9514 | 0.8500 |
| ai15.vector（方案1） | 0.0445 | 0.0048 | 0.0501 | 0.5000 | 0.4466 | 0.5000 | 0.6333 |
| base.vector（对照） | 0.0351 | 0.0095 | 0.0388 | 0.3750 | 0.4523 | 0.3750 | 0.5556 |

目标：Recall@20 ≥ 0.75、硬 ≥ 0.90、软 ≥ 0.65、Coverage ≥ 0.90、NDCG@10 ≥ 0.70、MRR ≥ 0.60、P@5 ≥ 0.75。
达标情况：**Coverage / NDCG@10 / MRR@20 / P@5 四项达标（混合模式），三项 Recall 全部未达标**（原因见第 4 节口径限制，非系统绝对召回结论）。

相对 `base.hybrid` 的增量：
- 方案1（ai15）：Recall@20 −0.0009、NDCG@10 −0.0012、P@5 ±0、MRR ±0 → **噪声级持平**。
- 方案2（rerank_prefixless）：Recall@20 −0.0021、NDCG@10 −0.0058、P@5 −0.0083 → **略微变差，无收益**。

逐查询看，混合模式 Recall@20 仅 2/24 项变化：Q16 `0.394→0.364`、Q17 `0.172→0.182`，其余 22 项完全相同。说明 15 字前缀在混合链路里对 top20 的影响极其有限（FTS 通道占主导，见第 3 节）。

向量模式是唯一出现方向性差异的地方：`ai15` 把 Coverage@20 从 `0.375` 提到 `0.500`、P@5 从 `0.5556` 提到 `0.6333`、Recall@20 从 `0.0351` 提到 `0.0445`（NDCG@10 略降 0.4523→0.4466）。即**前缀长度真正影响的是纯向量通道**，而该通道当前被阈值清空（见第 5 节）。

### 3. 方案2 为何近乎空操作（机制证据）

2400 条（24 查询 × top100）送进 reranker 的文档里，**只有 19 条命中已知画像前缀（0.8%）**，`unmatched=2381`。

全量核对 `base` 索引：`21673/21673` 条 child 的 `vector_text` 都以画像前缀开头；`1713` 条 parent 中仅 10 条偶合匹配。因此 `unmatched` 的 2381 条基本全是 **parent 文本 —— 它们本来就不带前缀**。

根因在链路结构，不在裁剪逻辑：
- `lancedb_index.search_fts(..., search_body=False)` 会强制追加 `chunk_type = 'parent'`（`lancedb_index.py:367-370`），混合检索的 FTS 通道**只回 parent**；
- 融合在 `_rrf`（rrf_k=60）后，FTS 排名靠前的 parent 在 top100 中占绝大多数，子切片只在向量排名极高时才能挤入；
- 而 reranker 输入取自 `hits[:100]` 的 `hit.vector_text`（`search/service.py:186`）。

**结论：在“仅 reranker 去前缀”的口径下，方案2 在当前生产链路中几乎是空操作，实测差异接近噪声，且三项指标略降。**

### 4. 口径限制（必须随结论一并引用）

1. **pool-limited Recall 有硬天花板。** 池由各变体 top100 ∪ 既有标签构成，池内 grade>=2 均值 `54.17 / 109.88`，故 Recall@20 的理论上限均值为 **0.4593**（逐查询 `min(1, 20/相关数)`）；24 个查询中 **23 个相关数 > 20**。实测 0.34 相当于该上限的 ~74%。同一系统同一排序在 `base.hybrid` 下 R@50 = **0.676**、R@100 = **0.923**，即该 FAIL 主要由 K 与分母的错配决定，而非系统绝对召回能力。作为交叉验证，把相关门槛收紧到 grade=3（强相关，n=23，均值 `24.9`，上限 `0.851`）后 R@20 = **0.475**，仍只达上限的 56%，说明口径能解释大部分差距但不能解释全部；逐查询 `R@100` 的真实缺口集中在 Q17 `0.626`、Q23 `0.592`、Q22 `0.606`（给到 100 名仍漏掉约 40% 池内相关人），属真实缺陷。
2. **硬条件目标在本池结构下不可达。** Q21/Q22/Q23 池内相关数分别为 `70 / 71 / 152`，Recall@20 上限约 `0.286 / 0.282 / 0.132`；0.90 目标不可能达成。且硬条件仅 **n=3**（软条件 n=21），分类指标统计意义有限。
3. **标签为模型代理标注。** 2419 条为既有盲评模型标签，218 条为本轮 DeepSeek 补判，**均非人工标注**；池内相关率偏高（~48%）也会同时抬高排序类指标。
4. **向量模式被绝对阈值清空。** 真实 BGE-M3 下 `base` 索引 Q01/Q02/Q03 的 top1 相似度为 `0.5861 / 0.5985 / 0.5591`，全部低于 `VECTOR_MIN_SIMILARITY = 0.6`，于是 `_apply_vector_threshold`（`search/service.py:272-282`）把结果全部过滤，纯向量模式多数查询返回 0 条；`ai15` 前缀把 Q01/Q02 top1 抬到 `0.6181 / 0.6334` 才恢复部分结果。
5. **单次运行、无重复与置信区间。** 方案2 仅扰动 19/2400 条文档，其 Δ 量级不宜作强结论。
6. Q25–Q27 无盲评标签，未纳入本轮。

### 5. 结论与处置

- **方案1（15 字前缀）：不落地。** 混合模式与现状无实质差异（−0.0009 Recall / −0.0012 NDCG），不足以支撑改 prompt 口径与索引重建成本；向量模式的小幅正向收益无法脱离阈值缺陷单独成立。
- **方案2（reranker 去前缀）：不落地。** 机制上近乎空操作（99.2% 输入本就无前缀），实测三项指标略降，无收益。
- **新增高优先级待办（本轮最有价值发现）：** `VECTOR_MIN_SIMILARITY = 0.6` 的绝对下限在真实 BGE-M3 + 当前简历语料上会把纯向量模式清空（Coverage@20 仅 0.375，即 62.5% 查询召回不到任何相关人）。这是“宁可多捞，不可漏掉”的直接违背项。建议改为以相对阈值为主（或按索引相似度分位数标定绝对下限），并在隔离副本上验证后再讨论落地；**本轮不动生产代码**。
- **Gate 6 结论不变：** 旧索引继续只读运行，不得切换到未通过的 dual_prefix 索引；阶段仍为**未完成**。

## 第十轮：`VECTOR_MIN_SIMILARITY` 标定与隔离验证（2026-09-18）

承接第九轮第 5 节待办，本轮按用户选定的「标定向量相似度阈值」推进：先用冻结索引实测 BGE-M3 分数分布标定绝对下限，再在隔离副本上验证阈值改动对向量/混合两层指标的真实影响。**未改产品源码**（阈值经 monkeypatch 在内存中替换），未触碰 `.dev-data`。

产出：`.tmp-prefix-eval/{vector_scores.json, threshold_calibration.json, threshold_verification.json, retrieval_threshold/*.json}`。
脚本：`scripts/calibrate_vector_threshold_2026_09_18.py`、`scripts/verify_vector_threshold_2026_09_18.py`。

### 1. 实测分数分布（24 查询，`base` / `ai15` 隔离索引）

| 变体 | top1 min | top1 中位 | top1 max | top1 < 0.6 |
|---|---|---|---|---|
| base（生产口径前缀） | 0.5030 | 0.5861 | 0.6589 | **15/24** |
| ai15（15 字前缀） | 0.5354 | 0.6073 | 0.7098 | 12/24 |

`VECTOR_MIN_SIMILARITY = 0.6` 高于实测 top1 的中位数，故纯向量模式在多数查询上被整体清空——第九轮的观测在 24 查询上得到确认。

### 2. 关键否定证据：向量分数与相关等级几乎不可分（`base`）

| grade | n | 均值 | p25 | p50 | p75 |
|---|---|---|---|---|---|
| 0 | 179 | 0.5258 | 0.5102 | 0.5233 | 0.5451 |
| 1 | 490 | 0.5303 | 0.5112 | 0.5282 | 0.5489 |
| 2 | 530 | 0.5403 | 0.5201 | 0.5399 | 0.5616 |
| 3 | 452 | 0.5436 | 0.5185 | 0.5395 | 0.5634 |

grade 0 与 grade 3 的均值差仅 **0.0178**（`ai15` 为 0.0241），且 **p75(grade=0) = 0.5451 > p50(grade=3) = 0.5395**，分布大幅重叠。任何绝对阈值都无法有效分离相关与不相关：阈值 0.48 时 precision 0.6047；拉到 0.58 时 precision 才 0.7857，但 recall 掉到 0.1639。**BGE-M3 原始余弦分数在当前语料上对本任务的判别力接近无效。**

### 3. 隔离验证：放开阈值能救活向量通道，但会拖垮混合模式

复用 B2 已建的隔离索引，仅在内存中替换 `service.VECTOR_MIN_SIMILARITY` / `VECTOR_RELATIVE_RATIO`；标签池冻结为既有 3 变体文件构成的池（2637 条），保证与第九轮数字可比。`extra` = top-k 中落在该冻结池外、无标签因而被计为不相关的候选数。

| 运行 | Recall@20 | 软 | 硬 | Coverage | NDCG@10 | MRR@20 | P@5 | 均返回 | 空查询 | 池外@10 | 池外@20 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base.vector @ 0.60（现状） | 0.0351 | 0.0388 | 0.0095 | 0.3750 | 0.4523 | 0.3750 | 0.5556 | 2.2 | **15** | 0 | 0 |
| base.vector @ 0.50 | 0.2438 | 0.2644 | 0.1000 | **1.0000** | 0.5729 | 0.8958 | 0.7083 | 60.7 | 0 | 34 | 109 |
| base.vector @ 0.45 | 0.2585 | 0.2811 | 0.1000 | 1.0000 | 0.5977 | 0.8958 | 0.7417 | 64.9 | 0 | 34 | 114 |
| base.vector @ 相对0.40 | 0.2574 | 0.2799 | 0.1000 | 1.0000 | 0.5943 | 0.8958 | 0.7417 | 64.9 | 0 | 35 | 114 |
| **base.hybrid @ 0.60（现状）** | **0.3414** | 0.3686 | 0.1507 | 1.0000 | **0.7157** | **0.9514** | **0.8583** | 100.0 | 0 | 0 | 0 |
| base.hybrid @ 0.50 | 0.3196 | 0.3488 | 0.1151 | 1.0000 | 0.6525 | 0.9028 | 0.8000 | 100.0 | 0 | 24 | 47 |
| base.hybrid @ 0.45 | 0.3189 | 0.3480 | 0.1151 | 1.0000 | 0.6543 | 0.9028 | 0.8083 | 100.0 | 0 | 24 | 48 |
| base.hybrid @ 相对0.40 | 0.3200 | 0.3493 | 0.1151 | 1.0000 | 0.6528 | 0.9028 | 0.8083 | 100.0 | 0 | 24 | 48 |

`ai15` 同向：向量模式 0.0445→0.2268（Coverage 0.5→1.0），混合模式 0.3405→0.3172、NDCG@10 0.7164→0.6457。

**结论出现反转：** 放开绝对下限确实让向量通道“恢复出结果”（向量模式 Coverage 0.375→1.0，空查询 15→0），但它同时**全面拉低混合模式**（NDCG@10 −0.063、Recall@20 −0.022、P@5 −0.058），且降幅远超方案1/方案2 的噪声量级。机制上可解释：向量分数不可判别（第 2 节），放开阈值等于向 RRF 注入低判别力的候选，挤掉了原本靠 FTS 排名进入 top100 的 parent。换言之，`VECTOR_MIN_SIMILARITY = 0.6` 在当前 embedding 上虽然“不符合分数分布”，却意外充当了**通道门控**，把无效通道压掉，使混合实际退化为 FTS + reranker——而该配置正是排序层唯一达标的配置（NDCG@10 0.7157 / MRR@20 0.9514 / P@5 0.8583 三项 PASS）。

### 4. 口径限制（随结论一并引用）

1. 第 3 节低阈值行的混合指标受**池外未标注候选**影响：top10 中有 24/240 个槽位（10%）落在冻结池外，被计为不相关。因此 −0.063 的 NDCG 降幅中有一部分可能是标签未覆盖造成的（该方向只会使低阈值行**偏低估**）。但第 2 节的判别力证据独立于该口径问题，机制结论不受影响。
2. 固定策略扫描显示，回落点在 0.48–0.50 之间取到 precision/recall 的折中（F1 峰值 0.825 出现在 0.48–0.50），与本节结论一致：可行区间很窄且收益为负。
3. 标签仍为模型代理标注（2419 prior + 218 DeepSeek 补判），非人工标注。
4. 单次运行、无重复与置信区间；`ai15` 的混合降幅（−0.023 Recall）大于方案1 在第九轮的噪声量级，但仍需重复验证。
5. Q25–Q27 无盲评标签，未纳入。

### 5. 结论与处置（含对第九轮第 5 节建议的修正）

- **不落地阈值改动。** 第九轮“建议改为以相对阈值为主”的待办，经本轮隔离验证**证伪**：恢复向量通道会使混合排序层从三项达标退化为三项下滑。`VECTOR_MIN_SIMILARITY = 0.6` 维持现状，**不改生产代码**。
- **向量通道在本 embedding 下不可救。** 判别力接近无效（grade 0 vs 3 均值差 0.018），调阈值、调相对比例都无法产生可用增益；提升向量通道需要换/微调 embedding 或引入打分校准，属独立课题。
- **方案1 / 方案2 一并关闭，不再优化。** 两者都只作用于已被证明无效的向量通道，收益上限被第 2 节封死。
- **口径复核优先于继续调参。** 三项 Recall 的 FAIL 主要由 K=20 与池内相关数（均值 54.2）错配决定（上限 0.4593）；混合排序层三项已 PASS。剩余真实缺口应单独定位 Q17/Q22/Q23（R@100 仅 0.592–0.626）。
- **Gate 6 结论不变：** 旧索引继续只读运行，阶段仍为**未完成**。

## 第十一轮：评测口径修正 —— 把「测量口径」与「真实缺口」分开（2026-09-18）

承接第十轮第 5 节「口径复核优先于继续调参」。本轮**零 API 成本、纯本地重算**既有变体文件，不重新检索、不改产品代码。落点：`scripts/prefix_eval_metrics_2026_09_18.py`（新增口径修正表与分类归一化），更新 `EVAL_DIR/summary.json`。

修正内容：① `recall20_cap = mean min(1, 20/n_relevant)` 给出当前池结构下 Recall@20 的**算术上限**；② `recall20_norm = recall20 / recall20_cap` 给出「在上限内实际拿回的比例」；③ `recall100` 作为**可比 K 参照**（本链路 reranker 只重排 `hits[:100]`，故 K=100 才是与链路匹配的召回口径）；④ grade 门槛分层（≥1/≥2/≥3）；⑤ 分类归一化（hard/soft 各自的 raw 与 cap，避免 3 条 hard 查询的均值被单点主导）。

### 1. 关键发现：原阈值的 Recall@20 目标在算术上不可达

| 变体 | Recall@20 | **recall20_cap（上限）** | recall20_norm | **Recall@100** | R@20 grade≥3 | **R@100 grade≥3** |
|---|---|---|---|---|---|---|
| base.hybrid | 0.3414 | 0.4593 | **0.7433** | **0.9226** | 0.4746 | 0.9193 |
| ai15.hybrid | 0.3405 | 0.4593 | 0.7413 | 0.9238 | 0.4610 | 0.9156 |
| rerank_prefixless.hybrid | 0.3393 | 0.4593 | 0.7387 | 0.9226 | 0.4665 | 0.9193 |
| base.vector | 0.0351 | 0.4593 | 0.0764 | 0.0351 | 0.0894 | 0.0894 |

扩展池内每条查询平均有约 54.2 个 grade≥2 相关项，因此 `mean min(1, 20/n_relevant) = 0.4593`，而判定阈值是 `recall20 >= 0.75`——**上限低于阈值，该指标在当前池结构下无论如何优化都必然 FAIL**。`recall20_hard` 同理更甚（阈值 0.90，hard 类上限仅 0.2330）。这三项 FAIL 中的「口径部分」由此定量确认，**不构成系统缺陷证据**。

### 2. 换成与链路匹配的口径后：排序层召回 92%，且对门槛不敏感

同一批结果，仅把 K 换成 100（= reranker 的重排窗口）后混合模式 **Recall@100 = 0.9226**；把相关门槛从 grade≥2 收紧到 grade≥3，数值几乎不动（0.9226 → 0.9193，`ai15` 0.9238 → 0.9156）。说明**高分档候选没有被系统性丢失**，剩余约 7.7% 的缺口不是「把强相关排到 100 名之外」造成的。

### 3. 分类归一化：soft 已在达标线，唯一真实短板是 hard 条件

| 变体 | hard 归一化（n=3, cap=0.2330） | soft 归一化（cap=0.4916） |
|---|---|---|
| base.hybrid | **0.6468** | 0.7498 |
| ai15.hybrid | 0.6468 | 0.7478 |
| rerank_prefixless.hybrid | 0.6468 | 0.7449 |

soft 条件归一化召回 0.7498，恰好落在 0.75 判定线上；hard 条件仅 0.6468，是**唯一在可比口径下仍然不达标的分层**。注意 hard 类 cap 仅 0.2330（每条 hard 查询池内约 86 个相关项），且 n=3，单条查询波动会直接放大到 0.33——该数字须与 n/cap 同读，不能单独作为结论。

### 4. 口径限制

1. 分母仍是**池受限召回**（扩展池 = 3 个已发布变体 top100 ∪ 既有标签，2637 条）。池外相关项未知，故 `recall100` 是当前证据下的**下限估计**，真实值只会更高。
2. `recall20_cap` 依赖池内相关数与 K 的关系，随池扩张会变化；本轮的 0.4593 只对应这个 2637 条冻结池。
3. 分类归一化的 cap 由 `recall20_soft_cap` 反推（0.4916），属派生量，未单独落盘字段之外。
4. 标签仍为模型代理标注，非人工标注；Q25–Q27 无标签未纳入。

### 5. 结论与处置

- **「三项 Recall FAIL」中属于测量口径的部分已定量剥离。** K=20 与池内相关数错配使上限只有 0.4593，低于 0.75 阈值——该 FAIL 是口径产物，不应再驱动调参。
- **可比口径下混合排序层召回 0.9226（grade≥3 为 0.9193），Soft 归一化 0.7498。** 现状配置在真实口径下接近达标，而非「差距很大」。
- **真实缺口收敛为一处：hard 条件（Q21/Q22/Q23）归一化 0.6468。** 加上第十轮已定位的 Q17，剩余缺口应在这 4 条查询上单独归因（检索输入/查询理解层面），而非在向量通道或前缀方案上继续加码。
- **方案1（15 字前缀）、方案2（reranker 去前缀）、阈值改动三者维持关闭。** 本轮进一步佐证：ai15 与 base 的 Recall@100（0.9238 vs 0.9226）与 NDCG@10（0.7164 vs 0.7176）差异在噪声内，`rerank_prefixless` 的 NDCG@10 还略低（0.7118）——三者均无收益甚至负收益。
- **Gate 6 结论不变：** 旧索引继续只读运行，阶段仍为**未完成**；后续优先级改为 hard 条件查询归因。

## 第十二轮：真实查询评测集 —— 用真实 JD 查询复核「差距是真实共性还是合成集特有」（2026-09-18）

承接第十一轮第 5 节「剩余缺口应单独定位」。前十一轮的结论全部建立在**合成查询集 + 模型代理标注 + 冻结标签池**之上，无法回答「这些差距在真实查询上是否同样存在」。本轮经用户授权使用真实 `.dev-data`（**只读直连，未复制、未重建、未调用 `optimize_pending()`；`.fts-dirty` 保持原样**），把真实历史 JD 查询脱敏后做成评测集，复用既有指标口径跑 keyword / vector / hybrid 三模式。

产出：`.tmp-real-eval/{real_queries.json, real_labels.json, label_stats.json, summary.json, retrieval/real_*.json, retrieval/real_*.meta.json}`。
脚本：`scripts/build_real_query_set_2026_09_18.py`、`scripts/run_real_eval_2026_09_18.py`、`scripts/real_eval_metrics_2026_09_18.py`。
数据源：`match_run` 中 `trigger=JD_MATCH` 的 `query_text` 去重（与 `jd_revision.parsed_data` 关联），候选人取 SQLite 人表与索引的交集。

### 1. 必须先读的口径偏差声明（不可与第一～十一轮数字横向比较）

初版标签沿用「全部 must 条目命中」的合取门（与第九～十一轮 `grade` 口径一脉相承）。实测在真实数据上该门**近乎不可通过**：通过率 min/median/max = **0.000 / 0.001 / 0.153**，即中位数每 1000 人里只有 1 人命中全部必备技能条目——这不是系统缺陷，而是「JD 必备技能条目 ∩ 1515 人小词表」的合取必然。

因此本轮把标签改为**有效条目覆盖率分级**（确定性、无 LLM、可复现）：
- 剔除「零命中」与「命中率 > 0.85」的条目（后者是通用词，无判别力）；
- `coverage = 命中条目数 / 有效条目数`；`grade = 3 (≥1.0) / 2 (≥0.6) / 1 (≥0.4)`。

修复后 99/99 查询可用。**结论：第十二轮的标签定义与第一～十一轮不同，两组数字只能比方向与排序，不能比绝对值。** 上一窗口「合成集 recall 已接近达标（0.92）」的说法，不能直接外推到真实查询。

### 2. 真实评测集规模与标签分布

```
universe(sqlite)=1519  indexed=1516  交集=1515
distinct query_text=120  其中带 jd_revision_id=100  标注查询=99  可用=99
分桶：hard=96 / mgmt=40 / plain=2（可重叠）
grade 分布（仅 >0）：grade1=17688  grade2=12994  grade3=3187
每查询正例数 min/median/max = 7 / 318 / 946
```

真实集的**相关集规模远大于合成集**（中位 318 人，占 1515 人的 21%），级联后果是 `recall@K` 被 `min(1, K/n)` 压制：`recall20_cap = 0.1895`、`recall100_cap = 0.4726`。上一轮确立的口径纪律（cap / 归一化必须同读）在真实集上同样成立，且**压制更严重**。

### 3. 三模式真实集指标（99 查询，limit=100，无硬条件过滤）

| 模式 | Recall@20 | cap | **norm** | Recall@100 | Recall@100 norm | R@20(g≥2) | R@20(g≥3) | R@100(g≥3) | NDCG@10 | MRR@20 | P@5(g≥2) | **Cov@20(g≥2)** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| keyword | 0.0744 | 0.1895 | **0.3926** | 0.2506 | 0.5303 | 0.0819 | 0.1467 | 0.4731 | **0.4431** | 0.6015 | 0.4768 | 0.8791 |
| vector | 0.0440 | 0.1895 | **0.2322** | 0.1075 | 0.2275 | 0.0708 | 0.1311 | 0.2876 | 0.4029 | 0.5927 | 0.4337 | 0.8571 |
| hybrid | 0.0699 | 0.1895 | **0.3689** | **0.2558** | **0.5413** | **0.0951** | **0.1606** | **0.5108** | 0.4388 | **0.6221** | **0.4909** | **0.9231** |

分桶（归一化 Recall@20，`norm`）：

| 模式 | hard (n=96, cap=0.1727) | mgmt (n=40, cap=0.2451) |
|---|---|---|
| keyword | 0.3648 | 0.2840 |
| vector | 0.2536 | 0.1999 |
| hybrid | **0.3688** | **0.2791** |

### 4. 结论：差距**是真实共性**，且比合成集更严重

1. **hard 条件是真实共性缺口。** 合成集里 hard 归一化 0.6468 已是不达标项；真实集 hard 归一化只有 0.365–0.369，**同一方向、量级更差**。第十一轮把 hard 条件（Q21/Q22/Q23）列为「唯一真实短板」的判定在真实查询上得到独立支持，不是合成集特有。
2. **管理岗（mgmt）是第二个真实缺口，此前未被识别。** mgmt 归一化 0.1999–0.2840，显著低于整体；NDCG@10 仅 0.319–0.341（整体 0.40–0.44）。这类查询的共同特征是「title 抽象、必备条目稀疏或泛化」，属查询理解层问题，而非通道问题。
3. **向量模式全面最弱（真实集复核）。** 归一化 Recall@20 0.2322 vs keyword 0.3926 / hybrid 0.3689；`recall100_norm` 仅 0.2275（即重排窗口内也捞不回）。第十轮「向量通道在本 embedding 下不可救」的结论在真实查询上再次成立，且 99 条中有 4 条纯向量返回**空结果**（`R004/R008/R043/R097`），与第九/十轮定位的 `_apply_vector_threshold` 绝对下限 0.6 一致。
4. **hybrid 综合最优，但优势并非压倒性。** hybrid 在 Cov@20(g≥2) 0.9231、MRR@20 0.6221、P@5 0.4909、R@100(g≥3) 0.5108 上均第一；**NDCG@10 反而是 keyword 略高**（0.4431 vs 0.4388）。说明混合链路目前的价值主要在「不空手」（覆盖率/命中率），而非首屏排序质量。
5. **强相关档（g≥3）整体召回很低**：三模式 R@20(g≥3) 仅 0.1311–0.1606，R@100(g≥3) 0.2876–0.5108。即「把最匹配的少数人排进前 20」这件事，真实场景下仍未解决。

### 5. 新增工程发现：hybrid 生产预算下约半数会话**静默丢失重排**

真实查询首次跑 hybrid 时 **53/99 查询返回 `RERANKER_UNAVAILABLE`**（keyword/vector 0 降级）。逐项排除「reranker 慢」「payload 大」「上游 burst 限流」等假设后，加 trace 抓到真实异常：

```
ProviderError: E_API_RATE_LIMIT: API 调用频率达到上限
```

生产 `search_timeout=8s`，且 `backend/src/kerui_recruit/search/service.py:197-198` 以 `except Exception: degraded.append("RERANKER_UNAVAILABLE")` **静默吞掉**该异常——**无重试、无退避、无 pacing、无任何用户可见提示**。评测用 `--retry 4 --search-timeout 60` 退避重试后 99/99 完成、0 降级。

含义：真实用户点「混合搜索」时，约一半会话实际拿到的是**未重排的 RRF 结果**，而结果页不会告知。这与「hybrid 排序层优于 keyword」的评价直接冲突——真实体验可能低于本轮 hybrid 指标。

### 6. 口径限制

1. 标签为**确定性规则派生**（必备技能条目覆盖率），非人工标注，亦非 LLM 标注；它奖励「技能条目命中」，对「能力等价但用词不同」的候选人会低估。
2. 分母是**全库 1515 人**（非池受限），故 `recall` 是真实召回而非池受限下限——这是本轮相对前十一轮的方法改进，但也使 cap 压制更严重（cap=0.1895）。
3. 检索**未注入 JD 硬条件过滤**（`CandidateFilters` 为空），测的是纯检索层；真实产品链路会先按年限/学历/城市过滤，故本轮 hard 桶数字不代表「开了过滤后的表现」。
4. 99 条查询中 96 条属 hard 桶，plain 桶仅 2 条，分桶均值对单点敏感；mgmt 桶 40 条，相对可靠。
5. 查询文本按 `hashlib.sha256(candidate_id)[:12]` 别名脱敏；查询原文未落盘，仓库内只保留脱敏 ID。
6. 单次运行、无重复与置信区间；向量空结果是**过滤行为**而非超时。

### 7. 候选优化思路（**仅登记，本轮不落地、不改生产代码**）

按「证据强度 × 成本」排序：

1. **（强证据 / 低成本）reranker 降级不可静默。** 上游限流是常态而非异常，当前 8s 预算 + 静默吞异常把「排序质量下降」变成用户不可见。最小改法：短退避重试 + 「本次结果未精排」的显式标记（`degraded` 已在契约里，只是没暴露给前端）；pacing/并发上限单独评估。
2. **（强证据 / 中等成本）hard 条件归因，而不是继续调检索参数。** 真实集已把缺口锁在 hard(0.365) 与 mgmt(0.20–0.28) 两类。下一步应拆「查询理解（`parse_query` 产出什么 keywords）→ 过滤条件是否错杀（第二/六轮已发现 MUST 误判：`985优先，必须熟悉Java`、`字节或阿里背景优先`）→ 排序」三段，逐段归因。
3. **（强证据 / 中等成本）mgmt 查询专项。** title 抽象、必备条目稀疏。可考察：title 是否进入检索信号、是否需要「管理跨度/团队规模」这类结构化信号，而非继续依赖技能词。
4. **（强证据 / 高成本）向量通道换 embedding 或做打分校准。** 第十轮已证判别力近乎无效（g0 vs g3 均值差 0.018），本轮真实集再次佐证。继续在当前 BGE-M3 上调阈值/加通道皆为负收益。
5. **（待验证 / 中等成本）子切片 FTS 通道。** 第一轮缺口 4 指出项目 chunk 只收 `tech_stack`/`summary`，缺 `name`/`business_scene`；真实集里 project 类证据的召回未被单独度量，建议先度量再决定。
6. **（结构性 / 高成本）schema 8→9 重索引与 Gate 6。** 属既有阻塞项，与本轮结论无冲突，优先级不变。

### 8. 结论与处置

- **第十一轮的「hard 条件为唯一真实短板」在真实查询上被独立证实，并新增 mgmt 为第二类缺口。** 合成集结论方向可信。
- **绝对数字不可外推。** 真实集归一化 Recall@20 仅 0.23–0.39（合成集曾达 0.74），差异主要来自标签定义与相关集规模，不构成「系统在真实查询上更差」的证据；但也说明**合成集上的达标不能作为真实场景的质量证明**。
- **vector 通道、方案1、方案2、阈值改动四项维持关闭**，本轮为其提供了第三份独立否定证据。
- **本轮唯一新增的工程缺陷（建议优先修）：reranker 限流降级静默化。** 它不改变检索算法，但直接决定真实用户体验——hybrid 的实际表现可能低于本轮指标。
- **未改任何生产代码；`.dev-data` 全程只读。Gate 6 结论不变：** 旧索引继续只读运行，阶段仍为**未完成**。

## 第十三轮：隔离实验 —— child chunk 进入候选池到底是「帮忙」还是「添乱」（2026-09-18）

第十二轮遗留一个未闭合的口径问题：生产 `search_fts` 显式加 `chunk_type='parent'`（见 `backend/src/kerui_recruit/search/lancedb_index.py:365-368`），而生产 `search_vector` **不做任何 chunk 类型过滤**（同文件 `:427-438`）。索引实测 `rows=22213`，其中 `parent=1722 / child=20491`（**child 占 92%**）。于是三模式的口径天然不对等：keyword 只在 1722 行 parent 上检索，vector 在 22213 行全量上检索，hybrid = parent FTS + 全量向量。第十二轮「vector 全面最弱」因此可能被两种相反的原因解释（向量本身弱 / 口径不等）。

本轮按用户指令做**单变量隔离实验**：只把向量通道限定 `chunk_type='parent'`，其余链路完全不变，直接量出 child 的净效果。

### 1. 实现方式（仅评测，未触碰生产代码）

`scripts/run_real_eval_2026_09_18.py` 新增两个变体，其余（查询解析、filters 为空、limit=100、指标口径、标签文件）与第十二轮**完全一致**：

- `vector_parent` = 生产 vector 行为 + 向量通道加 `chunk_type = 'parent'`；
- `hybrid_parent` = 生产 hybrid 行为 + 向量通道加同一条过滤（FTS 分支本来就是 parent-only，故等价于「全链路 parent」）。

落地方式为评测内子类 `_ParentOnlyVectorIndex(LanceDBSearchIndex)`，**只覆盖 `search_vector`**，复刻生产实现后仅追加 above 过滤；生产类零改动。运行参数 `--modes vector_parent,hybrid_parent --retry 4 --search-timeout 60`，两变体均 **99/99 完成、0 降级**（`degraded=0`），故不存在第十二轮第 5 节那种「一半会话丢失重排」的混淆。

### 2. 指标对照（99 查询，limit=100，无硬条件过滤）

| 变体 | Recall@20 | **norm** | Recall@100 | R@20(g≥2) | R@20(g≥3) | NDCG@10 | MRR@20 | P@5(g≥2) | P@10(g=3) | **Cov@20(g≥2)** |
|---|---|---|---|---|---|---|---|---|---|---|
| vector（生产，含 child） | 0.0440 | **0.2322** | 0.1075 | 0.0708 | 0.1311 | 0.4029 | 0.5927 | 0.4337 | 0.0832 | 0.8571 |
| vector_parent（去 child） | 0.0212 | **0.1119** | 0.0581 | 0.0389 | 0.0291 | 0.3198 | 0.4781 | 0.3972 | 0.0625 | 0.6813 |
| hybrid（生产，含 child） | 0.0699 | **0.3689** | 0.2558 | 0.0951 | 0.1606 | 0.4388 | 0.6221 | 0.4909 | 0.1212 | 0.9231 |
| hybrid_parent（去 child） | 0.0698 | **0.3683** | 0.2528 | 0.0993 | **0.1812** | **0.4486** | **0.6507** | **0.5172** | **0.1273** | **0.9341** |

候选池诊断（`real_*.meta.json`）：

```
                 n 中位数   n 均值   空结果   降级
vector              45      52.3      4       0
vector_parent        7      31.0     27       0     ← 池子被抽干
hybrid             100     100.0      0       0
hybrid_parent      100     100.0      0       0
vector  vs vector_parent：候选集 Jaccard 中位数=0.094，child 额外带来候选人中位数 +24
hybrid  vs hybrid_parent：候选集 Jaccard 中位数=0.754，child 额外带来候选人中位数 +14
```

逐查询配对检验（正数 = 含 child 更好）：

```
ndcg10  vector - vector_parent  均值差=+0.0916  se=0.0206  win/loss/tie=44/19/3  (≈4.4σ)
mrr20   vector - vector_parent  均值差=+0.1146  se=0.0401  win/loss/tie=26/16/57 (≈2.9σ)
ndcg10  hybrid - hybrid_parent  均值差=-0.0171  se=0.0087  win/loss/tie=32/42/21 (≈2.0σ)
mrr20   hybrid - hybrid_parent  均值差=-0.0286  se=0.0179  win/loss/tie=7/17/75
```

### 3. 结论

1. **child 进池是「帮忙」，而且在向量通道里是绝对主力，不能拿掉。** 去掉 child 后向量通道的候选集与原来**几乎不相交**（Jaccard 中位数 0.094），候选人中位数从 45 掉到 7，99 条里 27 条直接空结果（原来 4 条）。指标全面塌陷：归一化 Recall@20 0.2322→0.1119（−52%），R@20(g≥3) 0.1311→0.0291（−78%），NDCG@10 0.4029→0.3198，Cov@20 0.8571→0.6813。逐查询配对是 44 胜 19 负。
2. **因此第十二轮「口径不对等」的疑虑被证伪，且方向与猜测相反。** child 不是 vector 吃亏的原因，而是它唯一的信息来源——**在同一 1722 行 parent 词表上，vector_parent(0.1119) 远低于 keyword(0.3926)**。这说明短板确实是向量通道本身（与第十轮「当前 embedding 判别力近乎无效」一致），而不是「keyword 只搜了少量高质量行」。
3. **hybrid 下拿掉 child 有一点点好处，但量级很小且不稳定。** NDCG@10 0.4388→0.4486（配对 −0.0171，≈2.0σ，32 胜 42 负）、MRR@20 0.6221→0.6507、P@5 0.5172、Cov@20 0.9341 均略优，Recall@20 基本不变（0.3689→0.3683，因为召回由 parent FTS 承担）。合理解释是**重排输入粒度异构**：hybrid 的 100 条重排输入里，双通道候选人是 parent 摘要、仅向量召回候选人是 child 正文（`service.py:186` 取 `hit.vector_text or hit.content`），child-only 追加的候选人（中位数 +14）以不同粒度的文本参与排序，稀释了首屏。

### 4. 处置（按用户「如果是帮忙就加上」的授权）

- **不加 `chunk_type='parent'` 过滤。** 用户授权的判据是「child 帮忙则落地限制」，实测 child 在向量通道是决定性正贡献（结论 1），故**生产代码保持原样**，本轮不产生任何生产改动。
- **hybrid 那点提升不通过砍通道获取。** 若要做，正确做法是「重排输入粒度归一」（让候选人的重排文本统一取 parent 摘要），而不是让向量通道少召回——后者会把 vector 模式一起打死。此项**仅登记，未实施**。
- **附带发现（未验证影响，仅登记）：** `search_fts_boolean`（`operator=and/or` 分支，`lancedb_index.py:383-425`）**漏加** `chunk_type='parent'`，与 `search_fts`（`:365-368`）语义不一致，等于在这些模式下隐式强开正文检索。本轮评测走的是 `search_fts` 路径，未覆盖该分支。

### 5. 口径限制

1. 与第十二轮同源：标签为确定性规则派生（必备技能条目覆盖率分级），非人工/LLM 标注，**绝对值不可与第一～十一轮横向比较**。
2. 只做了一个变量（向量通道的 chunk 类型），未同时调整阈值；`vector_parent` 的塌陷里包含「parent 向量相似度低于 0.6 绝对阈值」的成分（median n=7、27 条空结果），但**这正是生产阈值下的真实后果**，不构成对结论的削弱。
3. `vector_parent` 的空结果 27 条中有 4 条是第十二轮已确认的空结果（R004/R008/R043/R097），其余为新增。
4. 两变体均 `degraded=0`，故与生产 hybrid 的「限流静默降级」问题无混淆；但生产 `search_timeout=8s` 下第十二轮第 5 节的缺陷依然存在，不影响本轮结论成立。
5. 单次运行、无重复；hybrid 配对的差异（≈2σ）在 99 条查询上属边缘显著，不宜当作确定性收益。

### 6. 未改生产代码；`.dev-data` 全程只读（未调用 `optimize_pending()`，`.fts-dirty` 保持原样）。第十三轮不改变 Gate 6 判定：**未完成**。

## 第十四轮：验证 `search_fts_boolean`（and/or 分支）的 chunk 口径缺陷（2026-09-18）

第十三轮第 4 节登记了一条**未验证的一致性缺陷**：`search_fts_boolean`（`operator=and/or`）漏加 `chunk_type='parent'`，与 `search_fts` 语义不一致。本轮按用户指令「验证一下」把它落到可复现的数据上。

验证脚本：`scripts/verify_fts_boolean_chunk_scope_2026_09_18.py`（只读，未触碰生产代码）。

### 1. 待验命题与静态证据

| # | 命题 | 静态证据（`backend/src/kerui_recruit/search/`） |
|---|---|---|
| 1 | `search_fts`（smart 分支）在 `search_body=False` 时隔离 child | `lancedb_index.py:365-370` 追加 `chunk_type = 'parent'` |
| 2 | `search_fts_boolean`（and/or 分支）**无**该过滤 | `lancedb_index.py:383-425` 全函数无 `chunk_type` 条件 |
| 3 | 该分支**不接收** `search_body`，只检索 `keyword_index_text` | 同函数签名与 `fts_columns=...` 字面量 |
| 4 | and/or 生产路径**不做** `_filter_by_hit_terms` | `service.py:227-233`（smart 分支在 `:236-238` 才做） |
| 5 | UI 可真实触发该组合（逻辑 and/or × 正文开关） | `TalentPoolPage.tsx:233-250`、`App.tsx:1423-1445` |

### 2. 实测（99 查询，limit=100，`.dev-data/search` 只读，`rows=22213`、含 parent chunk 的候选人 1516）

代表行构成（`search_fts` / `search_fts_boolean` 返回前均已按候选人去重 → 行即代表行）：

| 变体 | 行数 | child | **child 占比** | parent |
|---|---|---|---|---|
| `smart`（生产，body=off） | 29700 | **0** | **0.00%** | 29700 |
| `smart_body`（生产，body=on） | 29699 | 16549 | 55.72% | 13150 |
| `and_current`（生产 and） | 0 | 0 | – | 0 |
| `and_parent`（对照） | 0 | 0 | – | 0 |
| **`or_current`（生产 or）** | 29700 | **12086** | **40.69%** | 17614 |
| `or_parent`（对照） | 29700 | **0** | **0.00%** | 29700 |

候选池效应：`or` 模式下**仅靠 child 进入候选池的候选人共 6408 人**（其中 1193 人在索引里其实**存在** parent chunk，即被 child 代表行顶替/抢先），对照独有 6408 人，逐查询候选集 Jaccard 均值 0.6686，**99/99 条查询都出现 child 代表行**。`and` 两变体均为 0 行 → 该模式下 child 无影响。

排序指标（99 条有 concepts 的查询子集）：

| 变体 | R@20 | norm | R@20(g≥2) | R@20(g≥3) | NDCG@10 | MRR@20 | P@5(g≥2) | P@10(g=3) | empty |
|---|---|---|---|---|---|---|---|---|---|
| `smart` | 0.0744 | 0.3926 | 0.0819 | 0.1467 | 0.4431 | 0.6015 | 0.4768 | 0.1121 | 0 |
| `smart_body` | 0.0780 | 0.4116 | 0.0946 | 0.2086 | 0.4841 | 0.6764 | 0.5293 | 0.1455 | 0 |
| `and_current` | 0 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | **99** |
| `and_parent` | 0 | 0 | 0 | 0 | n/a | 0 | n/a | n/a | **99** |
| `or_current` | 0.0615 | 0.3245 | 0.0881 | 0.1376 | 0.4290 | 0.6610 | 0.5071 | 0.0990 | 0 |
| `or_parent` | 0.0617 | 0.3256 | 0.0788 | 0.1210 | 0.4169 | 0.6090 | 0.4788 | 0.0949 | 0 |

`or_current − or_parent`（child 的净效果）：NDCG@10 **+0.0121**、MRR@20 **+0.0520**、R@20(g≥2) +0.0093、R@20(g≥3) +0.0166、P@5(g≥2) +0.0283、P@10(g=3) +0.0041、R@20 −0.0002。

等价性抽查（生产 `search_fts_boolean` 带 60s `SEARCH_DEADLINE` 直跑 vs 复刻实现，alias 排序逐位比较）：

```
R001 or  True   生产行=300 复刻行=300（生产 0.1s）      R001 and True   生产行=0（生产 3.0s）
R002 or  False  生产行=300 复刻行=300（生产 0.1s）      R002 and True   生产行=0（生产 1.6s）
R003 or  True   生产行=300 复刻行=300（生产 0.1s）      R003 and True   生产行=0（生产 2.8s）
```

端到端探针（`HybridSearchService.search(mode="keyword")`，3 条查询 × {smart,and,or} × {body=0,1}）：

```
smart body=0 items=100 captured=[('bm25', 300)]        smart body=1 items=100 captured=[('bm25', 300)]
and   body=0 items=  0 empty=no_match captured=[('bm25', 0)]
and   body=1 items=  0 empty=no_match captured=[('bm25', 0)]
or    body=0 items=100 captured=[('bm25', 300)]        or    body=1 items=100 captured=[('bm25', 300)]
```

### 3. 结论

1. **命题成立（已验证）。** and/or 分支（`search_fts_boolean`）确实让 child chunk 直接进入候选池并成为代表行：`or` 模式 40.69% 的代表行是 child（99/99 条查询都出现），而 `smart`（`search_body=False`）为 0.00%。对照组 `or_parent` 的 child 占比为 0.00%，证明差异**唯一来自 chunk 口径**。
2. **但「补上 `chunk_type='parent'`」在 `or` 模式下会让指标变差，而非变好。** 与第十三轮向量通道的结论方向一致：child 在 `or` 通道同样是**净正贡献**（NDCG@10 +0.0121、MRR@20 +0.0520、g≥2/g≥3 召回均上升，`or_parent` 全线低于 `or_current`）。也就是说，当前代码不是「漏了过滤导致质量事故」，而是**靠这个不一致隐式获得了「正文检索」能力**。
3. **本轮真正暴露的两个缺陷与 child 无关，且更严重：**
   - **`and` 模式实质不可用**：99/99 条查询返回 0 结果、`empty_reason=no_match`（`and_current` 与 `and_parent` 均为 0，故与 chunk 口径无关）。原因是 `_matches_concepts` 的 `and` 语义要求**同一个 chunk 的 `keyword_index_text` 命中全部 concepts**，而真实查询解析出的 concepts 中位数在数十量级（本集 14–1347）→ 恒空。
   - **`search_body` 在 and/or 分支是死开关**：生产 `search_fts_boolean` 签名里没有该参数，服务层也不传。端到端探针显示 `body=0` 与 `body=1` 的 `items`/`captured` 逐条相同。即 UI 上「关键词逻辑=或」时，「检索正文」开关完全失效——`or` 恒等于正文开（child 40.69% 代表行）。
   - 附带性能事实：`and` 因首轮命中为 0 触发窗口倍增直至全表，单查询 1.6–3.0s（`or` 仅 0.1s）。
4. **处置（用户已裁决：维持现状、不改代码）**：最小修法本可以是给 `search_fts_boolean` 增加 `search_body` 参数，`False` 时追加 `chunk_type='parent'`（对齐 `search_fts`）并由 `service._keyword_retrieve` 透传；但按本轮数据该修法在 `or` 下会**降低**现有指标（NDCG@10 −0.0121、MRR@20 −0.0520），与第十三轮「child 帮忙则不限制」的同一判据冲突，故**不落地**。本轮登记、不修。`and` 模式恒空（99/99 空结果）与 `search_body` 在 and/or 下失效，是两个**独立于 chunk 口径**的可用性缺陷，同样只登记、待后续统一规划。

### 4. 口径限制

1. 标签口径与第十二/十三轮同源（确定性规则派生），**绝对值不可与第一～十一轮横向比较**，本轮的用法是**同口径变体间对照**（`or_current` vs `or_parent`、`and_current` vs `and_parent`）。
2. 复刻实现差异：为避免生产「窗口倍增 + 全列（含 1024 维向量）物化」导致单查询数十秒，`_boolean_rows` 用 `select` 精简列 + 单遍全表扫描复刻同一匹配/去重语义。抽查 6 次中 5 次逐位一致，`R002 op=or` 不一致 → 判为 FTS 在同一 BM25 分数下的 tie-break 对 `limit` 敏感，故本轮 `or` 的量化值带该噪声（同类噪声也体现在 `smart` 复现与 `real_keyword.json` 有 **2/99** 条不一致上）。
3. 未做重复运行，未做配对显著性检验（本轮差异量级明显小于第十三轮结论 1，故未按显著性结论处理，只作方向性判断）。
4. 本轮以离线复刻为主，生产接口仅做了 3 条查询 × 6 组合的端到端探针（足以证明分支可达与 `search_body` 无效，不足以代表全部 99 条）。
5. `.dev-data` 全程只读；未调用 `optimize_pending()`（`.fts-dirty` 保持原样）；未打印任何密钥；产物全为 sha256 别名。

### 5. 未改生产代码。第十四轮不改变 Gate 6 判定：**未完成**。

## 第十五轮：修复第十四轮登记的两个缺陷（and 恒空 + `search_body` 死开关）（2026-09-18）

第十四轮第 3 节登记了两个**与 chunk 口径无关**的可用性缺陷，并注明「只登记、待后续统一规划」。本轮按用户指令「请修复登记的两个缺陷」落地修复。

### 1. 语义裁决（用户已定，本轮据此实现）

| # | 待决问题 | 裁决 |
|---|---|---|
| 1 | `and`（「同时满足」）的语义 | **候选人级 AND + 只对技能类 concept 求与**；无策展概念时退化为 `or` |
| 2 | `search_body`（「检索正文」）默认值 | **保持默认关闭**，对齐 `smart`；接受 `or` 默认指标下降（第十四轮实测 NDCG@10 0.4290→0.4169、MRR@20 −0.0520），用户在 UI 打开即恢复 |

### 2. 生产改动（5 处，均在 `backend/src/kerui_recruit/search/`）

| # | 文件 | 位置 | 改动 |
|---|---|---|---|
| A | `lexicon.py` | `:134` | 新增公开谓词 `is_curated_concept(canonical)`：`canonical in _CONCEPT_ALIASES`（`:88` = `_SKILL_CONCEPTS` + `_GENERAL_SYNONYMS`）。未命中词表的通用词 canonical 即 token 本身 → 返回 `False` |
| B | `lancedb_index.py` | `:63` | 新增 `_matched_concept_indices(text, token_sets)`：返回该 chunk 命中的 concept 下标集合，供候选人级跨 chunk 聚合 |
| C | `lancedb_index.py` | `:389` | `search_fts_boolean` 重写：新增 `search_body: bool = False`；`search_body=False` 时追加 `chunk_type = 'parent'`（与 `search_fts:364` 对齐）；`operator='and'` 时只保留 `is_curated_concept` 为真的 concept，**为空则退化为 `or`**；`and` 转交 `_and_candidate_rows` |
| D | `lancedb_index.py` | `:453` | 新增 `_and_candidate_rows`：窗口倍增（`limit*3` 起，封顶 `row_count`）扫描，按 `candidate_id` 聚合各 chunk 命中下标，凑够 `limit` 个「覆盖全部待满足 concept」的候选人即返回；代表行取该候选人 BM25 排名最靠前的 chunk |
| E | `service.py` | `:221-230` | `_keyword_retrieve` 新增 `search_body=False` 形参并透传给 `search_fts_boolean`；`search(...)` 的 keyword 分支（`:168`）已传该实参 |

语义后果：`and` 从「单 chunk 必须命中全部 concepts」改为「候选人跨 chunk 覆盖全部**已策展** concept」，`keyword_index_text` 中出现大量通用词（负责/熟悉/经验）不再使查询结构性恒空；`search_body` 在 `and`/`or` 下由死开关变为真实开关。

### 3. 回归护栏（新增 5 个用例，`backend/tests/search/test_service.py`）

| 用例 | 断言 | 覆盖点 |
|---|---|---|
| `test_or_operator_respects_search_body[False/True]` | `()` / `('a',)` | `or` 下正文开关真实生效（概念只出现在 child） |
| `test_and_operator_is_candidate_level_and_respects_search_body[False/True]` | `()` / `('a',)` | 「java 在 parent、redis 在 child，无任何单 chunk 同时命中」仍返回 `a` → 候选人级聚合 |
| `test_and_operator_ignores_non_curated_generic_concepts` | `{a, b}` | `Java 负责` 只对 `java` 求与，只有通用词「负责」的 `c` 被排除 |
| seed 辅助 `seed_boolean_body_index` / `seed_boolean_generic_index` | – | 构造上述跨 chunk / 通用词场景 |

测试结果（`E:\RuanJian\python\python312\python.exe -m pytest`，工作目录 `backend/`）：

- 新增用例 `-k "search_body or candidate_level or generic_concepts"` → **7 passed**（含参数化展开与 2 个既有 smart 路径用例），22 deselected。
- `tests/search/test_service.py` + `tests/search/test_relevance_golden.py` → **33 passed, 1 failed**；失败项 `test_fts_returned_when_embedding_times_out`（`search_timeout=0.1`）**单独跑 1 passed**。
- `tests/search` 全量（deselect 3 个已知时序敏感用例）→ **182 passed, 1 failed**；失败项 `test_slow_reranker_keeps_fused_result` **单独跑通过**。
- 明确结论：`or` 基线（含 `test_relevance_golden.py`、第十四轮 `or_parent` = parent-only 口径）**无回归**。

时序敏感用例的外部噪声证据（与本次改动无关）：

- `test_timed_out_candidate_scan_does_not_start_more_native_queries` 预算 `.04s`；实测同场景 `is_ready()` 冷启动 **15–31ms**、`filter_search` 首次 **47ms** → 预算在扫描启动前即被吃光，`calls` 在 0/1 之间抖动（第十四轮该轮实测为 1）。
- 该用例走 `search("")` → `query.strip()` 为假 → `service.py:161-166` 的 `filter_search` 分支；`parse_query("")` 的 `concepts=()`，故 `service.py:167-168` 的 `search_fts_boolean` 分支**不可达**，本次改动不在其执行路径上。
- 本轮未修改任何生产/测试代码去「修」这些抖动，以免掩盖问题。

### 4. 真实集取证（`scripts/verify_boolean_and_search_body_fix_2026_09_18.py`，只读 `.dev-data/search`）

索引：`rows=22213`、`is_ready=True`、`is_compatible=False`；查询 99 条全部解析出 concepts（and/or 分支真实可达）。

**A. 全量扫描（select 精简列单遍复刻，`limit=100`，候选人数按生产口径去重）**

| 变体 | empty | empty 占比 | 候选人数合计 | 单查询均候选 |
|---|---|---|---|---|
| `and_legacy`（修复前：单 chunk AND + 全部 concept） | **99** | **1.0000** | **0** | **0.00** |
| `and_fixed_body0`（修复后，body=off，生产默认） | **4** | **0.0404** | 4675 | 47.22 |
| `and_fixed_body1`（修复后，body=on） | 4 | 0.0404 | 5096 | 51.47 |
| `or_fixed_body0` | 0 | 0.0000 | 9900 | 100.00 |
| `or_fixed_body1` | 0 | 0.0000 | 9900 | 100.00 |

- 缺陷①：`and` 的 `empty` 由 **99/99（100%）降到 4/99（4.04%）**，单查询均候选由 0 升到 47.22。残留 4 条是该查询的**已策展** concept 交集本就为空（诚实残留，非结构性恒空）。
- 缺陷②（`or`）：`body` 开关**改变结果的查询数 = 98/99**；`body=True` 多出的候选总数 = 0 —— 因 `limit=100` 已饱和，差异体现在**候选组成与排序**而非条数。

**B. 生产 `search_fts_boolean` 抽查（3 条 × and/or × body，`deadline=20s`，与复刻的 alias 排序逐位比对）**

```
R001 or  body=0 一致=True（0.2s）   R001 and body=0 生产行=  4 一致=True（0.3s）
R001 or  body=1 一致=True（0.1s）   R001 and body=1 生产行=  8 一致=True（1.2s）
R002 or  body=0 一致=False（0.2s）  R002 and body=0 生产行=  0 一致=True（0.1s）
R002 or  body=1 一致=False（0.1s）  R002 and body=1 生产行=  0 一致=True（0.3s）
R003 or  body=0 一致=True（0.2s）   R003 and body=0 生产行=300 一致=True（0.2s）
R003 or  body=1 一致=True（0.1s）   R003 and body=1 生产行=300 一致=True（0.1s）
生产 or 分支 body 开关裸对比（同一查询两次调用）:
  R001 body=0 行=300 body=1 行=300 相同=False
  R002 body=0 行=300 body=1 行=300 相同=False
  R003 body=0 行=300 body=1 行=300 相同=False
```

- 「裸对比」3/3 **不同** —— 第十四轮同一探针下 `body=0` 与 `body=1` 逐条相同，修复后开关在生产路径上真实生效。
- 唯一 `一致=False`（`R002 or`）**已定位为 BM25 同分 tie-break 的排序抖动，非正确性差异**：单独复核该查询得 `prod_rows=300 full_rows=300 win_rows=300`、`prod_ids ⊆ full_ids`、**交集 300/300**，仅前 100 位中第 74/75 位（对 full 还有 80/81 位）顺序不同。候选集合完全相同。
- 性能：修复后 `and` 生产耗时 **0.1–0.3s**（body=1 含 child 时最多 1.2s），第十四轮修复前为 **1.6–3.0s**（首轮命中 0 → 窗口倍增扫全表）。

**C. 端到端（`HybridSearchService.search(mode='keyword')`，`limit=100`）**

```
R001 op=and body=0 items=  4（0.3s）        op=and body=1 items=  8（1.2s）
R002 op=and body=0 items=  0 empty=no_match（0.1s）  op=and body=1 items=  0 empty=no_match（0.3s）
R003 op=and body=0 items=100（0.2s）        op=and body=1 items=100（0.2s）
R001/R002/R003 op=or body=0/1 items=100（各 0.2s）
```

端到端与 A/B 段数字自洽（`R001/R002/R003` 的 and 结果与 B 段生产行数一致），`degraded` 全为空 → 无静默降级干扰。

### 5. 结论

1. **缺陷① 已消除**：`and` 由 99/99 恒空（`empty_reason=no_match`）降为 4/99 空，单查询均候选 0 → 47.22；残留 4 条为策展概念交集为空的真实无解查询。生产耗时由 1.6–3.0s 降到 0.1–0.3s（窗口倍增不再打到全表）。
2. **缺陷② 已消除**：`search_body` 在 `and`/`or` 下真实生效（生产 `or` 裸对比 3/3 结果不同；`or` 全量 98/99 条查询结果随开关变化；`and` body=1 比 body=0 多召回 421 人）。默认值按用户裁决保持 **关闭**，与 `smart` 一致。
3. **`or` 无回归**：`tests/search` 全量除时序抖动项外全通过；`body=False` 时 `search_fts_boolean` 与 `search_fts` 的 parent 隔离语义现已一致。
4. Gate 6 判定不变：**未完成**（本轮只修可用性缺陷，未触及检索质量主线指标）。

### 6. 口径限制

1. 标签口径与第十二～十四轮同源（确定性规则派生），A 段数字**只用于同口径「修复前 vs 修复后」对照**，不可与第一～十一轮横向比较。
2. A 段 `or` 的 `body=True` 指标增益按用户裁决不落地（默认关闭），本轮**未重跑排序指标**（NDCG/MRR），增益/损失沿用第十四轮第 2 节实测值。
3. B 段 1/6 抽查出现排序抖动（同分 tie-break，候选集合 300/300 相同），与第十四轮第 4 节记录的同类噪声同源；A 段 `or` 量化值带该噪声。
4. 端到端仅 3 条查询 × 12 组合，足以证明分支可达与开关生效，不代表全部 99 条。
5. `.dev-data` 全程只读；未调用 `optimize_pending()`（`.fts-dirty` 保持原样）；未打印任何密钥；产物全为 sha256 别名。

### 7. 本轮修改了生产代码（`lexicon.py`、`lancedb_index.py`、`service.py`）与测试（`test_service.py`），并新增验证脚本 `scripts/verify_boolean_and_search_body_fix_2026_09_18.py`。第十五轮不改变 Gate 6 判定：**未完成**。
