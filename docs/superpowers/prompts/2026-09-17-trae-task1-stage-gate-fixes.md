# Trae「任务1」续做：第一阶段检索与匹配验收缺口

请在原「任务1」中继续修改当前 `candidate_pool` 源码。以源码和新鲜测试为准；不要因为 `search_quality_v2.json`、`match_quality_v2.json` 尚无人工标签而把本轮代码修复标为 blocked。第一阶段允许保留旧向量方案、明确写出消融收益未验证；实际结果的独立语义盲评和画像提示词 A/B 留给第二阶段。不要宣称未经盲评的准确率提升。

## 已复现的阻断项

1. **混合召回固定截断 30。** `backend/src/kerui_recruit/search/service.py:190-193` 在去重和按请求 `limit` 返回之前，把所有 hybrid 结果截为 30；`backend/src/kerui_recruit/match/service.py:117-123` 虽请求至少 100 个，实际最多拿到 30。搜索请求 `limit>30` 及分页也受影响。修复时让返回数量遵守请求的 `limit`、候选人去重和后续分页约定；匹配的第一轮可得到目标 50–100 个唯一候选人。重排器只处理前 100 条可以保留，但不应把返回池硬截为 30。加入大于 100 个候选命中的可重复测试，核验匹配打分至少能看到 80 个、搜索可继续翻页。
2. **多个 MUST 技能组没有逐组判断。** `backend/src/kerui_recruit/match/policy.py:69-75` 只在全部 MUST 为零命中时拒绝。实际纯函数复现：JD 为 `direction=BACKEND, required_skills=[Java, Spring]`，候选人只有 `Java`，`evaluate_pair` 返回 `eligible`，同时 `missing_skills=('Spring',)`。独立列出的两个 MUST 应都满足；同一个 OR 组（如 `Java or Kotlin`）满足任一即可；组内 `Java and Spring` 要求两者。若原 JD 无法确认某项确为 MUST，应标待核，不要擅自硬拒。增加按组资格测试，且总分不能补偿确认的 MUST 缺口。
3. **人找 JD 绕过资格拒绝。** `backend/src/kerui_recruit/match/service.py:360-404` 的 `_reverse_records` 只用方向等条件和 `score.total >= 0.4` 过滤；`_score_context` 虽计算 `eligibility`，没有用其过滤。与 JD 找人 `_score_and_sort` 的行为不一致。两入口应调用同一资格判断；`rejected` 不进入推荐，`pending` 独立呈现，不能混入推荐或静默视作合格。加入同一候选人/JD 配对的正反向测试：方向与年限通过但零 MUST 命中时两边都不能推荐；一项 MUST 缺口、方向待核也要测。
4. **职责对召回和排序基本不起作用。** `backend/src/kerui_recruit/match/service.py:495-505` 的 JD 查询只拼全部技能，有技能时不带核心职责，也没有抽取 3–5 个区分度高的概念；`:145-179` 的匹配总分只用技能、重排、年限、方向、行业，`policy.py` 的 `duty_evidence` 没有参与排序。请做最小、可解释的调整：词法通道使用少量关键技能/职责概念，语义通道涵盖 JD 核心职责；同资格、同技能覆盖时，有真实项目/工作职责证据者应优先于仅列技能名者。不要让已通过的方向、年限固定奖励淹没职责差异。用两名技能相同但职责证据相反的样例验证排序和证据回指；若词法证据不足，标待核，不要编造经历。
5. **可选 AI 复核结果不可逐项使用。** `backend/src/kerui_recruit/match/review.py:136` 的 verdict 仅附 `candidate_id`；人找 JD 的同一候选人有多 JD，无法区分 verdict 属于哪个 JD。`desktop/src/pages/JdManagementPage.tsx:306-318` 和 `desktop/src/App.tsx:3539-3551` 成功后只显示推荐/待核/不推荐总数，匹配行不显示逐项结论与证据。请在每个复核结果带稳定的 `match_result_id` 和 `jd_revision_id`，按匹配行显示 AI 结论、关键证据/缺口及失败或待核状态。基本结果始终先返回，复核默认关闭；失败不应覆盖基本结果。以两个 JD 相反 verdict 的反向匹配用例和前端渲染用例验证不会串项。

## 完成口径

- 优先修复以上会影响召回、资格、排序和 AI 结果可用性的缺口，保持现有 API 和页面的合理兼容。不要做无关重构或产品级打磨。
- 跑聚焦后端测试、前端测试及 `npm run build`；记录命令、通过数、失败及可复现性。当前独立检查中，前端 103 项通过、构建通过；后端聚焦 122 通过、1 个 0.1 秒超时用例失败，单独重跑通过，暂视为时间敏感测试，不能把它等同上述确定性缺口。
- 更新 `docs/verification/search-match-quality-2026-09-16.md` 和 `.trae/search-match-quality-progress.md` 的真实状态与证据。缺人工标注的向量变体仍记为“语义收益未验证”，不因此停止第一阶段功能修复。完成后明确报告仍未验证的部分，供下一阶段独立盲评。
