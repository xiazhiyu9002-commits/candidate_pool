# 批量操作、双形态画像与人岗复核 — 行为/准确度基线（2026-09-17）

> 实施前冻结：本轮（Task 1）基线快照。评测命令与结果均可复现。
> 上一轮验收：`docs/verification/2026-09-17-search-match-v2-acceptance.md`（纯向量 P@5 未达 0.60，保留旧生产索引，主流程混合检索）。

## 1. 检索/匹配/方向准确度基线（真实复跑结果）

命令（仓库根目录）：

```powershell
py -3.12 scripts\evaluate_semantic_quality_v3.py --baseline
```

结果：**8/8 项通过**。

| 指标 | 值 |
| --- | --- |
| keyword P@5 / P@10 / nDCG@10 | 0.7167 / 0.6875 / 0.5682（queries=24） |
| vector P@5 / P@10 / nDCG@10 | 0.375 / 0.2708 / 0.2655（空结果 6） |
| hybrid P@5 / P@10 / nDCG@10 | 0.8583 / 0.8583 / 0.7077（空结果 0） |
| 正向（JD 找人）hybrid 配对 | 14 |
| 反向（人找 JD）hybrid 配对 | 5 |
| 已标注配对判定分布 | recommend 14 / pending 7 / reject 2 |
| 方向库内标签与模型一致 | 40/52 |

## 2. 四个页面/两个画像当前真实行为

- 人才库列表与搜索结果共用 `CandidateTable`，每行单条操作：匹配、查看详情、建流程、解析表、下载、强制 OCR、删除。**无多选/批量**。
- JD 岗位列表：单条「匹配候选人」「解析表」「删除」。**无多选/批量删除**。表头「推荐理由」+「注意点」两列已显示 AI 复核 reasons/cautions。
- 招聘流程列表（`RecruitmentPage`）：单条删除。**无多选/批量删除**。
- 反向匹配抽屉（人找岗位）：AI 复核当前把 reasons/cautions 压成单个 `note` 文本显示；**无「查看详情」列**、**未按复核结论排序**。
- 简历 `ai_profile_summary` 与 JD `candidate_profile` 均为单段文本；索引已有父/子 chunk（候选人父=整份、子=工作/项目经历；JD 父=整体、子=职责+技能），但**画像分点未作为独立子切片、无 compact_context 前缀、无证据路径**。

## 3. 关键契约与失败用例（本轮新增需满足）

- 批量选择按稳定实体 ID（`candidate_id` / `jd_id` / `case_id`），表头「全选」仅当前显示页；搜索/筛选变化清空选择。
- 多选匹配=逐人执行一次「人找岗位」基础匹配并汇总，**不自动触发 AI 深度复核**。
- 强制 OCR 单条当前请求体为 `force_ocr:false, use_vision:true`（语义错误），需修正为批量动作发 `force_ocr:true, use_vision:false`。
- 反向匹配每行 `revision_id` 是 **JD 修订 ID**；需另带该次匹配使用的 `resume_revision_id` 才能打开正确简历原件。
- 同一事实在整体/分点画像不得冲突；人工修改后重解析/回填不覆盖；生成失败不清空已保存双形态。
- 明确硬条件（卡 985、字节或阿里、LangGraph 优先）：MUST 不能被向量高分补偿；「优先」只加分；「或」为组内 OR。
- 职业方向保留兼容主方向，新增 `specializations` 多值细分标签，旧数据缺新标签不得产生新硬筛漏检。

## 4. 回归锚点

```powershell
# 后端聚焦回归（上一轮 173 passed 的覆盖范围）
$env:PYTHONPATH='src'; py -3.12 -m pytest tests/match/ tests/direction/ tests/jd/ tests/resumes/ tests/search/test_sync_unified_view.py -q

# 前端
npm test -- --run
npm run build
```

## 5. 已知未达标项（承接上一轮，不因本轮启动而消失）

1. 纯向量 P@5 未达 0.60（最优 0.5917）。
2. 52 张方向盲卡与人工复核标签一致率未测（`headhunter_grade`/`adjudicated_grade` 为 null）。
3. Gate 5 误荐降级未在冻结 10 JD / 5 候选人样例复跑。
4. `tests/api/test_jd_flow.py` 6 个既有失败（与本轮无关）。
