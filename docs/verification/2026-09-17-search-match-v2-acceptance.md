# 检索与双向人岗匹配准确度改造 — 阶段验收报告（2026-09-17）

> 计划：`docs/superpowers/plans/2026-09-17-search-match-accuracy-v2.md`
> 评测依据：`docs/verification/2026-09-17-semantic-audit.md`
> 进度记录：`.trae/search-match-accuracy-v2-progress.md`

## 结论摘要

已完成 Task 1–5 核心改造与 Task 6 阈值消融、Task 7 的前端分层展示与方向审计/验收。**核心漏检（前端方向空值、通用词/可替代框架误硬筛、多简历证据不一致）已被修复**；纯向量模式的准确度门槛（P@5 ≥ 0.60）**未达标**，按计划保留生产向量阈值，主流程继续走混合检索。

关键改进（相对 2026-09-17 基线）：

| 维度 | 基线 | 现状 |
| --- | --- | --- |
| 前端 JD 方向空值漏检 | 0 人（方向等值过滤拦截 3 位直接相关者） | 救援通道救回 ✅ |
| 数据工程「关系型数据库」漏检 | f96cb81456e3↔d13894a1 被拒 | eligible（同义映射）✅ |
| 前端「前端工程化」漏检 | 42c0b000fc46↔56f942a1 被拒 | eligible（同义映射）✅ |
| 智能体架构师 asyncio/CrewAI 过度硬筛 | 9eeb2bf3737c↔543afa81 全库 0 | 旧 required_skills 软特征，不再全库漏检 ✅ |
| 纯向量 P@5 | 0.375（6/24 空） | 最优 0.5917（阈值消融，未达标 0.60） |

## Gate 逐项验收

### Gate 1 — 可重复评测门槛 ✅
- `scripts/evaluate_semantic_quality_v3.py --baseline`：**8/8 PASS**。
- keyword P@5=0.7167、vector P@5=0.375、hybrid P@5=0.8583、正向 14 对、反向 5 对、方向 40/52。
- 夹具 `backend/tests/fixtures/{search,match}_quality_v3.json` 覆盖 24 搜索意图 + 15 匹配样例，标签可追到脱敏证据，无姓名/联系方式。

### Gate 2 — 职业方向主/次判断 ✅（核心）
- `direction` 向后兼容；新增 `direction_assessment`（primary/secondary/confidence/evidence_paths/management，taxonomy v2）。
- 三项已知边界（iOS→FRONTEND、数据平台→BACKEND、Agent→BACKEND）不再高置信误分（`tests/direction/test_classifier_v2.py`）。
- **诚实记录**：确定性关键词分类器在 52 张盲卡上与模型标签一致率 24/52（低于模型 40/52）。这是回退/重判用的确定性分类器，主分类靠更新后的 LLM 提示词；与「人工复核标签」的一致率因人工复核尚未执行而暂无真值，**不宣称 ≥90%**。

### Gate 3 — JD 必备技能 AND/OR 组 ✅
- `must_skill_groups`（组内 OR、组间 AND）+ 同义映射（关系型数据库↔MySQL/Oracle/SQL、前端工程化↔Webpack/组件库/微前端）。
- 旧 `required_skills` 作为软特征（至少命中一项才不排除），避免旧解析把可替代框架拆成独立 MUST。
- 冻结配对回归（`scripts/verify_frozen_pairs.py`）：
  - f96cb81456e3↔d13894a1：eligible（可入选）✅
  - 42c0b000fc46↔56f942a1：eligible（至少待核）✅
  - 9eeb2bf3737c↔543afa81：eligible（不再全库漏检；缺指定框架留待核）✅

### Gate 4 — 统一多份简历人选证据 ✅
- `match/candidate_view.py::build_candidate_view`：硬字段取最近修订，技能/经历/项目跨修订去重合并。
- `_candidate_parsed_data` 与 `_candidate_representation` 统一使用该视图；`search/sync.py::_snapshot` 对同一候选人的各 chunk 写统一 direction/location/total_years/highest_degree。

### Gate 5 — 双通道召回与职责证据重排 ✅（核心，部分未在冻结样例复跑）
- `recall.py::merge_recall_lanes`：同向通道 70 + 救援通道 30（取消方向过滤），按 candidate_id/jd_id 去重。
- `_direction_assessment`：同向 recommend、次向 0.5/needs_review、低置信不处罚；`_score_context` 权重为职责 35%/技能 25%/语义 20%/资历 10%/方向 10%。
- `MatchScore.match_tier`（recommend/needs_review）+ API `match_tier/direction_reason/evidence`。
- 集成测试：FRONTEND JD 通过救援通道救回 3 位方向空值前端人选 ✅。
- **诚实记录**：「项目业务场景/规模 20%」未做独立评分组件（用语义相关性近似）；「数据分析误荐不在推荐层」「前端岗误荐降级」依赖 match_tier + 职责证据，已实现但未在冻结 10 JD / 5 候选人样例上复跑。

### Gate 6 — 纯向量阈值消融 ❌（未达标，按计划不切换）
- `scripts/evaluate_vector_thresholds.py` 在冻结索引上重放 24 查询，网格对比绝对阈值 0.45/0.50/0.55/0.60 × 相对比率 0.85/0.90/1.00。
- 最优纯向量 P@5 = **0.5917**（abs=0.45/rel=0.85，0 空结果），**未达 0.60 门槛**。
- 结论：**不切换生产向量阈值/文本**，保留 0.60/0.90。A/B/C 索引文本变体消融未做（需隔离索引重建 + 全量 re-embedding，成本高；且阈值消融已证明纯向量瓶颈不在阈值而在索引文本，混合主流程 P@5=0.8583 达标）。

## 已知漏检/误荐案例最终结果

| 案例 | 基线 | 现状 |
| --- | --- | --- |
| 56f942a1 前端开发（3 位方向空值前端）| 0（召回前方向等值过滤）| 救援通道救回 ✅ |
| d13894a1 交易运营数据工程负责人 | 0（“关系型数据库”误硬筛）| eligible ✅ |
| 543afa81 智能体架构师 | 0（asyncio/CrewAI 独立 MUST 全库 0）| eligible（软特征，缺框架待核）✅ |
| c8cc7ea1 到店业务数据分析（2 位 DATA 误荐）| 误荐 | match_tier=needs_review 可分层，未在冻结样例复跑 ⏳ |

## 未达标项与阻碍

1. **纯向量 P@5 未达 0.60**（最优 0.5917）。阻碍：当前父向量文本含学校/城市/年限/公司等噪声字段，仅调阈值无法达标；A/B/C 变体需隔离索引重建 + 全量 re-embedding（约 1713 修订 × 多个 chunk），成本高，未执行。按计划保留生产索引，主流程混合检索。
2. **52 张方向盲卡与「人工复核标签」一致率未测**。阻碍：人工复核尚未执行，`headhunter_grade`/`adjudicated_grade` 仍为 null，不能虚报。
3. **Gate 5 误荐降级未在冻结 10 JD / 5 候选人样例复跑**。已实现 match_tier + 职责证据分层，但未在冻结快照上做端到端复跑验证。
4. `tests/api/test_jd_flow.py` 6 个既有失败（Local JD pipeline 未产出 OPEN/READY、`/api/soft-delete` 404），与本改造无关，未修。

## 回归与可复现命令

```powershell
# 后端聚焦回归（173 passed）
$env:PYTHONPATH='src'; py -3.12 -m pytest tests/match/ tests/direction/ tests/jd/ tests/resumes/ tests/search/test_sync_unified_view.py -q

# 可重复评测基线（8/8 PASS）
py -3.12 scripts\evaluate_semantic_quality_v3.py --baseline

# 冻结配对回归（3/3 eligible）
py -3.12 scripts\verify_frozen_pairs.py

# 纯向量阈值消融（结论：不切换）
py -3.12 scripts\evaluate_vector_thresholds.py

# 前端（104 passed + build 成功）
npm test -- --run
npm run build
```

## 最终验收判定

- 已知直接相关的前端/数仓漏检被救回 ✅
- 双向同配对资格一致（统一 evaluate_pair + candidate_view）✅
- AI 深度复核默认关闭、基础结果不依赖 AI ✅
- 纯向量 P@5 ≥ 0.60 **未达标**（0.5917）❌，按计划保留旧生产索引，继续以混合检索 + 职责重排解决主流程。
- 任务**未全部完成**：Task 6 的 A/B/C 索引变体消融、52 卡人工复核标签、Gate 5 冻结样例复跑仍需后续推进。
