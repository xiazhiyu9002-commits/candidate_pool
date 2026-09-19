# Trae「任务1」续做：第一阶段最后的资格与 AI 复核门槛

请在原「任务1」继续。上一轮五项修复已覆盖，但独立复查发现两条可复现的错误放行路径，第一阶段仍未通过。只修核心正确性，避免额外重构；缺人工标注的向量消融仍留给第二阶段，不阻塞本轮确定性修复。

## 1. 评分异常时不得把未审核召回结果当作匹配结果

`backend/src/kerui_recruit/match/service.py:129-133` 在 `_score_and_sort` 抛出任何异常时执行 `scored = hits`，返回 `empty_reason=None, degraded_reasons=()` 的正常结果。此时 `evaluate_pair` 的方向、MUST 等资格判断完全未运行；`api/match.py:74-82` 还可能把它记成正常 match_run。独立复现：在 `tests/match/test_reverse_index.py::setup` 的有效 JD/候选人夹具上让 `_score_and_sort` 抛 `RuntimeError('scoring failed')`，`match_jd('rev-a')` 仍返回 `['person']`，没有错误/降级标记。

请改成失败关闭：评分或资格检查失败时不返回未经检查的候选人，不保存正常 run；明确返回 `service_error`/降级原因，或抛可被 API 正确处理的错误。加一条候选人不满足 JD MUST、评分步骤失败的端到端测试，确认 `items=[]`、错误可见、没有“推荐”和 match_run。也检查反向路径的同类宽松异常回退。

## 2. 可选 AI 复核不得把无证据或非法状态验收为“推荐”

`backend/src/kerui_recruit/match/review.py:79-89,168-184` 的模型字段是普通 `str`，`_validated` 仅核验 ID 是否存在，不核验 `verdict` 与 `duty_checks.status` 的允许值、是否覆盖每项 JD 核心职责，且允许 `recommend` 携带空 `duty_checks`。独立纯函数复现：

```python
MatchReviewService._validated(
    ReviewVerdictModel(verdict="recommend", duty_checks=[], reason="fits"),
    {"d0"}, {"e-exp-0"})
# 当前返回 recommend，未检查 d0
```

`status="invented"` 也会被接受。请让服务实际使用的 `_validated` 路径严格校验枚举、职责 ID 覆盖及证据 ID；职责有缺项、非法状态、没有证据却声称直接满足时，结果应标 `pending`/`review_failed`，不能输出“推荐”。加聚焦测试验证合法多职责输入通过、空职责核查和非法状态不能推荐、单对失败不破坏其余基础匹配。

## 3. 待核资格与复核理由在结果上明确可见

`match/service.py:221-225,409-412` 只剔除 `rejected`，`pending` 仍可能进入基础匹配结果及后续 AI 复核。`desktop/src/pages/JdManagementPage.tsx:389-396`、`desktop/src/App.tsx:3633-3636` 逐行只显示 AI 标签，理由仅藏在鼠标悬停 title，`missing` 与职责证据未呈现。请保证 `pending` 不能被当成“推荐”、不能被 AI 推翻为已通过硬条件；复用既有方向待核入口或单独标示其状态，避免静默丢失。逐行展示简短的复核理由/缺口，并保留基础结果与 AI 结论的区别。用候选人方向待核与两 JD 相反复核结论的用例核对前后端。

## 验收与报告

重新跑聚焦后端测试、前端测试与构建，更新 `docs/verification/search-match-quality-2026-09-16.md` 和 `.trae/search-match-quality-progress.md`。本次独立检查：前端 **104 passed**、构建通过；后端 **241 passed, 3 failed**，其中两个 0.04/0.1 秒时序用例单独重跑 **2 passed**，另一个为既有 `/api/soft-delete` 404。不能把时序用例或人工标注缺失当作上述错误放行路径的修复。完成后报告可复现证据与仍未验证的语义指标。
