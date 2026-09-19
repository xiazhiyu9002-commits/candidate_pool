# Trae「任务1」续做：方向待核仍被当作正常匹配

上一轮评分异常失败关闭、AI 复核严格校验已通过独立聚焦检查。现在只剩一条可复现的阶段阻断：**基础匹配仍把资格 `pending` 的人放进普通匹配名单**。请在原任务修复此项并重跑相关测试；不要展开无关重构或要求先补人工语义标注。

## 复现与影响

`backend/src/kerui_recruit/match/service.py:223-228` 的 `_score_and_sort` 仅跳过 `rejected`；`pending` 仍按总分进入 `page.items`。反向 `_reverse_records` 在 `:410-413` 也仅排除 `rejected`。`backend/src/kerui_recruit/api/match.py:74-82` 将这样的正向结果写成正常 run；`desktop/src/pages/JdManagementPage.tsx` 虽在顶部统计“方向待核”，但这一行仍在普通匹配列表中，且可“建流程”。AI 复核阶段跳过 pending 不会修复基础名单的误荐。

我用 `tests/match/test_reverse_index.py::setup` 的有效索引/JD/候选人夹具复现：只把当前 `ResumeRevision.parsed_data.direction` 改为 `None`（索引里仍是 BACKEND），调用 `match_jd('rev-a')` 返回 `items=[('person', eligibility='pending')]`、`empty_reason=None`。这是索引方向和当前结构化数据有时差时的典型路径，不能把待核当作方向硬筛已通过。

## 最小验收要求

1. 同一 `evaluate_pair` 结论在 JD 找人与人找 JD 两入口一致：只有 `eligible` 进入推荐/可建流程/可选 AI 复核；`rejected` 排除；`pending` 明确标成待核并可从已有方向待核入口找到，或独立呈现但禁止当作已推荐。不要静默把待核算成“无匹配”。
2. 加聚焦回归：当前候选人方向为 `None` 或 `OTHER`、索引仍保留旧 BACKEND 时，正向匹配不得返回正常推荐或写入正常推荐 run；混合有效候选人和待核候选人时有效者仍正常返回，待核者状态明确。反向入口和前端“建流程”动作同样验证。
3. 新鲜运行后端匹配/搜索关键测试、前端测试和构建，更新 `docs/verification/search-match-quality-2026-09-16.md` 与 `.trae/search-match-quality-progress.md`。本轮独立聚焦测试 `137 passed`、前端 `104 passed`、构建通过，但它们没有覆盖上述时差样例；不要用现有绿灯替代修复证据。

缺人工标注的向量消融与真实语义准确度仍留下一阶段盲评；这次只处理待核误荐。完成后报告具体返回契约和前端行为。
