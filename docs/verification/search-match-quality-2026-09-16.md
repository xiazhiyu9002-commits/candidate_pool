# 搜索与人岗匹配质量改造 — 验证报告（2026-09-16）

## 一、代码回归结果

后端全量回归（`pytest -q --ignore=tests/search/test_sync.py --ignore=tests/soft_delete/test_soft_delete.py`）：

```
863 passed, 6 failed, 2 skipped, 1 deselected, 1 warning
```

6 个失败均为**计划执行前已存在**的既有问题，与本改造无关：

| 失败 | 根因 | 性质 |
| --- | --- | --- |
| `tests/cases/test_case_service.py::test_job_delete_and_restore_pause_only_unfinished_linked_reminders` | `soft_delete/service.py` 缺失 | 既有（模块缺失） |
| `tests/api/test_jd_flow.py::test_candidate_soft_delete_is_rejected` | `/api/soft-delete` 返回 404 而非 400 | 既有（端点缺失） |
| `tests/providers/ai/test_probes.py::test_discovery_intersection_selects_same_role_catalog_model` | `role_models` 缺 `FAST_TEXT` 键 | 既有 |
| `tests/search/test_consistency.py::test_timed_out_candidate_scan_does_not_start_more_native_queries` | 时序敏感 | 既有 flaky |
| `tests/search/test_service.py::test_slow_reranker_keeps_fused_result` | 时序敏感 | 既有 flaky |
| `tests/api/test_full_acceptance.py::test_desensitized_sample_end_to_end` | 任务超时 | 既有 flaky |

前端回归：`npm test -- --run` → 101 passed（10 files）；`npm run build` → 成功。

本改造期间修复的问题：`tests/direction/test_policy.py` 与 `tests/match/test_policy.py`、`tests/direction/test_backfill.py` 与 `tests/backfill/test_backfill.py` 存在 pytest 模块名冲突（导致全量收集 `import file mismatch`），已分别重命名为 `test_match_policy.py`、`test_direction_backfill.py`。

## 二、必过用例核验

| 必过用例 | 实现 | 测试证据 |
| --- | --- | --- |
| 北京大学不触发北京现居硬筛 | Task 2 `_is_school_entity` | `test_query.py::test_school_entity_peking_is_not_location` |
| 正文开关前后范围正确 | Task 3 `search_fts` chunk_type 隔离 | `test_service.py::test_search_body_off_excludes_child_chunks` / `test_search_body_on_includes_child_chunks` |
| AND/OR 技能组正确 | Task 9 `_OR_CONNECTORS` 修正 | `test_match_policy.py::test_and_skill_requires_both_tokens` / `test_or_skill_requires_any_token` |
| 无 MUST 证据不能被分数补偿 | Task 9 `_score_and_sort` 硬拦截 | 代码 + `test_match_policy.py` 技能组用例 |
| 方向待核不静默丢失 | Task 7/8 `direction` 归一为 `None` + `evaluate_pair` pending | `test_direction_backfill.py` / `test_match_policy.py::test_evaluate_pair_direction_missing_is_pending` |
| 双入口同对一致 | Task 10 `evaluate_pair` 纯函数 | `test_match_policy.py::test_evaluate_pair_*` |
| AI 关闭无远程复核调用 | Task 12/13 复核默认关闭 | `api/match.py` start 幂等 + 前端按钮默认不触发 |

## 三、指标对照（未完成，需人工标注）

以下量化指标**尚未验证**，因为 Task 1 生成的两份标注集仍是「待人工标注」模板：

- 搜索 `Recall@50/100`、`Precision@10`、`nDCG@10`、字段筛选误伤数、p50/p95
- 匹配 `Precision@5/10`、硬条件违规数、核心职责证据正确率
- AI 开关前后增益、单次复核时延与成本

按计划约定：「没有人工标签时可以完成代码基线，但不得宣称质量提升」。

## 四、发布门槛评估

- 硬条件违规 0：代码层已通过 `_score_and_sort` 拦截 + `evaluate_pair` rejected，但**数值验证需标注集**。
- 必过用例全通过：✅（代码 + 测试证据见上）。
- 指标不退化且至少一项改善：❌ 未验证（需标注集）。
- p95 在交互预算内：❌ 未验证（需性能测试）。
- 索引迁移/回滚演练：❌ 未执行（需 shadow 索引 + 生产切换演练）。

## 五、结论

**代码基线已全部落地**（Task 1–13 的确定性修复与 AI 复核前后端），通过 RED→GREEN→回归并记录于 `.trae/search-match-quality-progress.md`。**质量提升声明与最终发布门槛需补齐人工标注集后方可核验**。

## 六、第一阶段验收缺口修复（2026-09-17）

独立代码审查发现第一阶段检索/匹配仍有 5 项确定性缺口，已逐项修复（TDD：先失败测试 → 最小实现 → 回归）。缺人工标注仅影响向量消融的语义收益，不阻塞以下确定性功能。

### 6.1 修复项与测试证据

| # | 缺口 | 修复 | 测试证据 |
| --- | --- | --- | --- |
| 1 | hybrid 召回固定截断 30 | 移除 `search/service.py` 的 `HYBRID_RERANK_TOP_K` 硬截断，返回数量遵守请求 `limit` 与候选人去重 | `test_service.py::test_hybrid_respects_limit_beyond_30`（80 个候选人全量返回） |
| 2 | 多个独立 MUST 只命中一项仍判 eligible | `policy.py::evaluate_pair` 改为「任一 MUST 缺口即 rejected」（AND/OR 由 `_skill_hit` 按连接词判定） | `test_match_policy.py::test_evaluate_pair_independent_must_skills_require_all` / `test_evaluate_pair_or_group_accepts_any` |
| 3 | 人找 JD 绕过资格拒绝 | `_reverse_records` 排序前过滤 `score.eligibility == "rejected"`，与 JD 找人 `_score_and_sort` 一致 | `test_reverse_index.py::test_reverse_rejects_zero_must_skill` |
| 4 | 核心职责不参与召回/排序 | `_query_text` 并入 `core_duties`；`_score_context` 新增 `duty` 分量（证据覆盖率加权），`breakdown` 带 `duty_evidence` | `test_match.py::test_duty_evidence_ranks_above_skill_only` |
| 5 | AI 复核结果不可逐项使用 | `review_run` 的 verdict 新增 `match_result_id` 与 `jd_revision_id`；前端匹配行按行显示推荐/待核/不推荐 | `test_review_service.py::test_review_verdicts_carry_result_and_jd_ids` + `App.test.tsx::shows per-row AI verdicts in candidate match drawer` |

### 6.2 回归结果（2026-09-17）

后端聚焦（`pytest tests/search tests/match tests/direction tests/scheduler tests/api/test_jd_flow.py tests/api/test_backfill.py tests/api/test_direction_pending.py --ignore=tests/search/test_sync.py`）：

```
243 passed, 2 failed
```

2 个失败均为既有/时序问题，与本轮修复无关：

- `tests/api/test_jd_flow.py::test_candidate_soft_delete_is_rejected`：`/api/soft-delete` 端点缺失（`soft_delete.service` 模块缺失），既有。
- `tests/search/test_consistency.py::test_verified_fts_exclusions_survive_embedding_timeout`：0.15s 超时时序敏感，单独重跑通过（既有 flaky）。

前端：`npm test -- --run` → **104 passed**（10 files）；`npm run build` → 成功。

### 6.3 仍未验证的部分（供下一阶段独立盲评）

- 向量变体 A/B/C 与 JD 变体 B 的**语义收益未验证**：需冻结快照 + 生产 BGE-M3 模型 + 人工标注（`search_quality_v2.json` / `match_quality_v2.json` 仍为空标签）。
- 搜索 Recall/Precision/nDCG、匹配 Precision/硬条件违规数、AI 开关增益、p95 延迟等量化指标未计算（依赖标注集）。
- 索引迁移/回滚演练未执行（只有在消融证明收益、需要切向量版本时才进行）。

## 七、最后资格与 AI 复核门槛修复（2026-09-17）

独立复查发现两条可复现的错误放行路径，已逐项修复（TDD）。缺人工标注的语义指标仍留第二阶段。

### 7.1 修复项与测试证据

| # | 缺口 | 修复 | 测试证据 |
| --- | --- | --- | --- |
| 1 | 评分异常时把未审核召回结果当正常结果返回 | `match_jd` 的 `_score_and_sort` 异常改为失败关闭：返回 `empty_reason="service_error"` + `SCORING_UNAVAILABLE`，不再 `scored = hits`，也不写正常 run | `test_reverse_index.py::test_match_jd_fails_closed_on_scoring_error` |
| 2 | AI 复核可接受 recommend 却无职责核查/非法 status | `review._validated` 严格校验 verdict/status 枚举、职责 ID 覆盖、证据 ID、direct 必须有证据；缺项/非法 → `pending` | `test_ai_review.py::test_validated_rejects_*` 5 用例 + `test_validated_accepts_full_recommend_with_evidence` |
| 3 | pending 资格混入推荐、复核理由未逐行显示 | `review_run` 对 `eligibility != "eligible"` 的配对跳过 AI 直接 `pending`；前端逐行显示结论 + 简短理由/缺口 | `test_review_service.py::test_review_run_does_not_recommend_pending_eligibility` + `App.test.tsx::shows per-row AI verdicts in candidate match drawer`（断言「推荐 · 有证据」「不推荐 · 无证据」） |

### 7.2 回归结果（2026-09-17）

后端聚焦（`--ignore=tests/search/test_sync.py`）：

```
251 passed, 2 failed
```

2 个失败均为既有/时序问题，与本轮修复无关：

- `tests/api/test_jd_flow.py::test_candidate_soft_delete_is_rejected`：`/api/soft-delete` 端点缺失（`soft_delete.service` 模块缺失），既有。
- `tests/search/test_service.py::test_fts_returned_when_embedding_times_out`：0.1s 超时时序敏感，单独重跑通过（既有 flaky）。

前端：`npm test -- --run` → **104 passed**（10 files）；`npm run build` → 成功。

### 7.3 仍未验证（供下一阶段独立盲评）

- 向量变体 A/B/C 与 JD 变体 B 的语义收益未验证（需冻结快照 + 生产 BGE-M3 + 人工标注）。
- 搜索 Recall/Precision/nDCG、匹配 Precision/硬条件违规数、AI 开关增益、p95 延迟未计算。
- 索引迁移/回滚演练未执行。

## 八、方向待核不再进入正常匹配（2026-09-17）

独立复查发现：基础匹配仍把资格 `pending` 的人放进普通匹配名单（`_score_and_sort` / `_reverse_records` 只剔除 `rejected`）。已修复，只修此阶段阻断。

### 8.1 返回契约

- `evaluate_pair` 用 `is_pending_direction` 判定：方向缺失/OTHER/非法值 → `pending`（不再把 OTHER 当方向不匹配的 `rejected`）。
- JD 找人与人找 JD 一致：只有 `eligible` 进入推荐/可建流程/可选 AI 复核；`rejected` 排除；`pending` 不进入 `items`，不写入正常 run。
- 正向匹配当召回全部为 `pending` 时返回 `empty_reason="candidate_direction_pending"`（不静默当作 `no_match`）；API 不写空 run。
- 前端 `runJdMatch` 对 `candidate_direction_pending` 给出「匹配到的候选人均为方向待核，请在人才库方向待核中确认方向后再匹配」提示；待核者无「建流程」按钮（不在匹配列表内）。

### 8.2 测试证据

- `test_reverse_index.py::test_forward_match_excludes_pending_direction`（None/OTHER）：`items=()`、`empty_reason="candidate_direction_pending"`。
- `test_reverse_index.py::test_reverse_excludes_pending_direction`（None/OTHER）：反向 `records == []`。
- `test_match.py::test_match_keeps_eligible_and_excludes_pending`：混合时有效者正常返回、待核者排除。
- 修复既有夹具占位方向：`test_match.py` 候选人补 `direction=BACKEND`、`test_scheduler.py` 的 `direction="Java"` → `"BACKEND"`。

### 8.3 回归结果（2026-09-17）

后端聚焦（`--ignore=tests/search/test_sync.py`）：

```
257 passed, 1 failed
```

1 个失败为既有：`tests/api/test_jd_flow.py::test_candidate_soft_delete_is_rejected`（`/api/soft-delete` 端点缺失，soft_delete 模块缺失）。

前端：`npm test -- --run` → **104 passed**；`npm run build` → 成功。

### 8.4 仍未验证（供下一阶段独立盲评）

- 向量变体 A/B/C 与 JD 变体 B 的语义收益未验证（需冻结快照 + 生产 BGE-M3 + 人工标注）。
- 搜索/匹配量化指标与索引迁移回滚演练未执行。

## 十、方向与 MUST 技能改为「分类即匹配结果」（2026-09-17）

用户确认：方向不需要「待核」环节，解析时用 AI 从描述判定最符合的具体方向，直接作为匹配结果；不准时后期手动调整。据此去掉方向待核阻塞（**本节覆盖/取代第九节中「方向待核排除」的结论**），并修复 MUST 技能过度指定导致的空结果。

### 9.1 根因（实测数据）

- 方向待核阻塞：候选人 1718 中 364 空 + 140 OTHER = 504 待核（29%）；OPEN 岗位 80 中 46 空 + 2 OTHER = 48 待核（60%）。
- MUST 技能过度指定：73/80 个 OPEN 岗位 `required_skills + MUST技能` 超过 7 个；「Java/Python/Go 之一」被解析成 3 个独立必备技能，叠加「任一 MUST 缺口即拒绝」→ 几乎无人匹配。

### 9.2 修复

| 文件 | 改动 |
| --- | --- |
| `match/policy.py` | `evaluate_pair` 仅当两侧方向都确认且不一致才 rejected；`_must_skills_from_parsed` 只取 `required_skills` 并上限 5 |
| `match/service.py` | 移除 `match_jd` 的 `jd_direction_pending` 早返回；`_hard_filter`/反向不再对 null/OTHER 方向硬过滤；`_must_skills` 只取 `required_skills` 并上限 5 |
| `match/review.py` / `runtime.py` | 只跳过 `rejected`（回退 pending 跳过） |
| `providers/generation_tasks.py` | direction 提示词总是输出具体方向；required_skills 提示词限制 3~5 个并「之一」写 OR |

### 9.3 返回契约

- 方向未确认（null/OTHER/非法）不参与硬过滤，仍按技能/职责返回候选人；两侧方向都确认且不一致才拒绝。
- MUST 硬条件只取自 `required_skills`（前 5 个），不再纳入 `requirements` 里的长短语 MUST。

### 9.4 回归结果

后端聚焦（`--ignore=tests/search/test_sync.py`）：

```
256 passed, 2 failed
```

2 个失败均为既有/时序：`test_candidate_soft_delete_is_rejected`（soft_delete 404）、`test_evidence_read_obeys_same_deadline`（0.06s 时序，单独重跑通过）。

前端未改动（契约兼容）；`npm run build` 成功、`npm test -- --run` 104 passed（历史）。

### 9.5 实测验证（运行后端）

- 「软件工程师」岗位（BACKEND，13 个 required_skills）正向匹配：**no_match → success，返回 10 人**（分数 0.86~0.92）。
- 反向匹配（候选人→JD）仍为 0：根因是 JD 索引 `.dev-data/search/jobs` 未构建（与方向/MUST 无关），需触发 JD 索引同步。

### 9.6 仍未验证（留第二阶段独立盲评）

- 向量变体语义收益、搜索/匹配量化指标、索引迁移回滚演练。
- JD 索引构建后，反向匹配（候选人→JD）需重新验证。


## 九、独立阶段验收（2026-09-17）

Codex 在 Trae「任务1」明确结束后重新读取当前源码，并独立运行以下检查；本节只判断第一阶段的确定性功能与明显错误，不宣称真实语义准确率已经达标。

- 后端：`py -3.12 -m pytest tests/search tests/match tests/direction tests/scheduler tests/api/test_jd_flow.py tests/api/test_backfill.py tests/api/test_direction_pending.py tests/api/test_filter_only_request.py tests/api/test_pagination.py --ignore=tests/search/test_sync.py -k 'not test_candidate_soft_delete_is_rejected' -q` → **261 passed, 1 deselected**。排除项是既有 `/api/soft-delete` 404，与本次搜索/匹配门槛无关。
- 前端：`npm test -- --run` → **104 passed**；`npm run build` → 成功。
- 复现回归：用 `tests/match/test_reverse_index.py::setup`，将当前简历 revision 的方向改为 `None` 而保留索引中的旧 `BACKEND`，`match_jd('rev-a')` 现在返回 `items=[]`、`empty_reason='candidate_direction_pending'`；此前同一场景曾返回 `('person', eligibility='pending')` 的普通匹配结果。
- 源码复查：hybrid 不再固定截断 30；独立 MUST 技能组逐组判断；两方向匹配仅把 `eligible` 放入推荐；评分异常失败关闭；AI 深度复核由前端显式触发，服务校验职责与证据引用，逐行关联匹配结果。

**第一阶段结论：阶段性通过。** 已达到当前约定的核心功能和明显误筛/误荐错误修复标准，可以停止 Trae 实施监控并启动第二阶段独立语义盲评。搜索/匹配的真实 Precision、Recall、nDCG、AI 增益、提示词画像质量与向量变体收益尚无独立标注结论；这些由第二阶段检查，不能把本节通过解释为它们已改善。旧向量方案在缺乏消融证据时继续保留。


