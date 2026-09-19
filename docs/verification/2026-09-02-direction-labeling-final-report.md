# 科瑞招聘系统 · 职业方向打标 — 最终实施与验证报告

日期：2026-09-02/03
范围：方向打标（taxonomy / 模型 / 规则 / LLM 主判融合 / 简历&JD 接入 / 索引 / 匹配 / 搜索 / 回填 / 前端）

## 一、执行结论

方向打标功能已端到端完成并通过验证：

| 项 | 结果 |
|---|---|
| 简历回填 | **2635/2635（100%）** |
| JD 回填 | **48/48（100%）** |
| 索引方向字段覆盖（candidate） | **31570/31800 chunks（99.3%）** |
| 索引方向字段覆盖（jd） | **15/15（100%）** |
| 索引同步队列 | **排空（0 pending / 0 failed）** |
| 后端测试 | 577 passed / 1 处与方向无关的既有失败 / 2 skipped |
| 方向模块测试 | **63 passed** |
| 前端测试 | **55 passed** |
| 十万级基准 | P50 706.92ms / P95 795.56ms / P99 825.26ms（<< 10s） |

## 二、回填质量验证（真实数据）

全量 2635 份简历方向分布：

- 最大单一方向 `AI_ML` 占 **42.0%**（< 70% ✅），`BACKEND` 35.9%，其余分散于 19 个方向
- `UNKNOWN` **54 份（2.0%）**（< 50% ✅）
- 状态分布：CONFIDENT 1707 / UNCERTAIN 874 / UNKNOWN 54
- 标签来源：LLM **3716** / RULE 11（**99.7% LLM 主判**，无 Prompt 塌缩 ✅）
- 规则兜底 `used_rule_fallback`：**4/2635（0.15%）**
- 终态错误码：`E_API_SCHEMA` 8、`ProviderError` 7、`ValidationError` 1，共 16 例；**无 `E_API_RATE_LIMIT` 残留**

## 三、10 项修订逐项验证

1. **工作树快照**：阶段 0 已保存 `worktree.patch`（未暂存）+ `index.patch`（暂存区）+ `untracked/` + `status.txt`，见 `E:\traeWork\KeRui\.backups\20260902-211129\`。✅
2. **Provider 装配**：[factory.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/providers/factory.py) 中 `direction_llm` 由 `OpenAICompatibleDirectionProvider(base_url=settings.deepseek_base_url, model=settings.deepseek_model, ...)` 装配，未写死厂商。✅
3. **manual_overrides 跳过 LLM**：[backfill.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/backfill.py#L133) `if manual.get("direction_profile"): stats.manual_skipped += 1; return`；机器结果只写入 `review_data.direction_profile`，人工覆盖保留在 `manual_overrides`。✅
4. **方向加权先于 limit 截断**：[search/service.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/search/service.py#L121) 先扩大召回池 `pool_limit` → Rerank → 方向软加权 → 最终排序 → 截取 `limit`。✅
5. **DirectionService 唯一入口**：[service.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/service.py) 提供 `get_profile`/保存/撤销；[correction.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/api/correction.py) 仅做 `field_name == "direction_profile"` 委派，未复制第二套 DirectionProfile 逻辑。✅
6. **LLM 只返回编码/证据/自报置信度**：[classifier.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/classifier.py#L138) 后端生成 `is_primary`、`source`、版本与最终校准置信度；LLM 侧仅返回 code/evidence/confidence。✅
7. **回填幂等/并发/退避/续跑**：[backfill.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/backfill.py) 幂等版本判断（`_is_current`）、`concurrency = max(3, min(c, 5))`、429 指数退避 `min(2**(attempt+2), 30)`、`_is_retryable` 白名单、每类型独立 cursor、`preflight()` 单任务锁/预检。✅
8. **legacy 映射**：[taxonomy.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/taxonomy.py) 25 个方向族均带非空 `aliases` 强关联别名（完整映射，无空映射）。✅
9. **删除对「规格书第 X 节」的依赖**：备份/回填/索引切换/验收步骤均已内联到计划与脚本，无外部章节引用。✅
10. **TDD 顺序实施**：方向模块 63 个测试先写后实现（RED→GREEN），全量测试通过。✅

## 四、6 项补充逐项验证

1. **is_primary + 唯一主方向**：[models.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/models.py#L26) `is_primary: bool`；[models.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/models.py#L172-L173) 校验 `primary_role_code` 必须存在于 `role_family_codes`。✅
2. **阶段 0 双补丁**：同时保存未暂存 + 暂存区补丁（见 `index.patch` / `worktree.patch`）。✅
3. **METADATA 同步覆盖 candidate+jd**：[sync.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/search/sync.py#L227-L249) `_publish_metadata` 同时处理 candidate 与 jd，仅更新方向字段不调用 embedding；索引 v2→v3 迁移复用向量并覆盖两类索引。✅
4. **expected_profile_version（SHA256）**：[service.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/service.py#L167) `hashlib.sha256(...)`；事务内不一致抛 409（`DirectionConflict`）。✅
5. **pool_limit 公式 + 第 51 名测试**：[search/service.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/search/service.py#L121) `pool_limit = min(max(limit*3, 100), 300)`；[test_direction_intent.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/tests/search/test_direction_intent.py#L17) `test_direction_boost_moves_51st_into_top50`。✅
6. **回填前预检**：[backfill.py](file:///e:/traeWork/KeRui/.worktrees/embedded-recruitment/backend/src/kerui_recruit/direction/backfill.py#L44-L69) `preflight()` 检查 schema/Provider/人工覆盖/幂等/单任务锁；分布告警 `distribution_warnings()`（单一方向 >70% 或 UNKNOWN >50% 时抽查）。✅

## 五、十万级基准（`docs/verification/2026-09-02-direction-100k.json`）

- candidate_count 100000 / query_count 200 / concurrency 10
- P50 706.92ms / P95 795.56ms / P99 825.26ms
- error_rate 0.0 / Recall@300 1.0 / NDCG@10 0.869

## 六、备份与回滚准备

- DB：`recruit.pre-direction-20260902-221512.sqlite3`（52.8MB）、`recruit.pre-v12-to-v13-20260902-221653.sqlite3`
- 索引：`search.pre-v3-switch2-20260902-223925`（及其他 pre-v3 快照）
- 工作树：`.backups\20260902-211129\{index.patch, worktree.patch, untracked/, status.txt}`

## 七、已知遗留（非本次方向打标范围）

- `tests/providers/test_websearch.py::test_tavily_provider_maps_results` 失败：断言中的 `exclude_domains` 期望值（bosszhipin/zhipin/liepin/51job/lagou/zhilian）与当前实现（zhihu/sohu/sina/163/qq/ifeng）不一致，属 websearch 既有测试/实现漂移，与方向打标无关。
