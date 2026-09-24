# AI 复核改造：输入源、输出结构与三档判定（2026-09-21）

> 目标：让 AI 复核（匹配复核 / 搜索复核）只看真正决定成败的东西——技术、业务、项目；
> 输出只留两列可用信息；三档结论（推荐 / 待核 / 不推荐）有明确、可解释、可回查的判据。
>
> 本文只描述改造方案与已定决策，不含实现细节之外的新设计。

## 1. 背景与现状问题

现状（改造前）：

| 问题 | 现状事实 |
| --- | --- |
| 输入源不对 | 匹配复核用的是 **JD 解析结果**（`summary` + 画像 + `core_duties` + 加分行业/项目类型 + 技能），既不是画像人工版，也不是 JD 原文 |
| 权重是平的 | `_build_jd_text` 把 summary、画像、职责、加分、技能平铺，没有「职责/优先项 > 任职要求」的优先级 |
| 硬筛信息混入 | 搜索复核的输入包含学历/年限/城市/QS 等**已在第一步筛过**的条件 |
| 输出含冗余 | 逐条要求清单与「满足/部分满足/不满足/未见证据」结论，占字数且不是使用者要的信息 |
| 判据缺失 | `verdict` 没有标准，全靠模型即兴；前置资格被拒却标成「待核」；模型自相矛盾（三段全空却说推荐）也无兜底 |

实测数据（设计依据）：

- JD 原文（31 条现役）最短 138、**中位 767**、p90 975、**最长 1022** 字，**无一条超过 2000 字**，空原文 0 条；
- 搜索复核历史结论仅 1 条。

结论：**当前不需要主动截断**（原文与现有的解析结果拼接量级相当）；需要的是**结构标注**（为优先级服务），截断只作将来超长 JD 的兜底。

## 2. 已定决策

| # | 决策 |
| --- | --- |
| 1 | 复核输入**剔除硬筛**（学历/学校层次/年限/年龄/性别/城市/QS/薪资，含搜索复核的筛选项）；**保留公司名作背景**（不作为评分维度） |
| 2 | 「画像是否人工改过」判据 = `manual_overrides.candidate_profile` 存在（人工编辑保存本来就会写它）。今天被清掉的历史 20 个 JD 按未改过处理，走 JD 原文分支 |
| 3 | 匹配复核输入源：人工改过 → **画像文本**；未改过 → **JD 原文（按小节标注）** |
| 4 | 搜索复核输入源：关键词 + **软性条件**；剔除硬筛字段 |
| 5 | 输出固定三段 `project_match` / `experience_match` / `tech_match` + `risks`；前端两列：符合点列 = 三段按顺序拼接，风险点列 = `risks` |
| 6 | 保留 `verdict`、证据编号（`projects[0]` 这类）、`failed`/`error` 失败标记 |
| 7 | 删除逐条要求清单与其结论 |
| 8 | 不主动截断；仅 >6000 字时按小节优先级兜底裁剪，绝不从句子中间截断 |
| 9 | 前置资格被拒（方向不一致 / 必备技能缺失）→ **直接判 `reject`**，并写明原因 |
| 10 | 一致性兜底：三段全空却给推荐 → 降 `pending`；给不推荐但三段有实质证据 → 保留并标记 |
| 11 | 结论附 `basis` 结构化字段（**不进 UI**），供排查「为什么判成待核」 |

## 3. 输入契约

### 3.1 候选人侧（`build_candidate_text`，两侧共用）

| 保留 | 剔除 |
| --- | --- |
| `summary`、`current_title`、`current_company`（背景用）、`industry`、`skills[]` | `name`、`total_years`、`highest_degree`、`educations`、`school`、`school_level`、`qs_rank`、`age`、`gender`、`location`、`preferred_location` |
| `experiences[0..4]`：`title @ company` + `summary` | 画像文本（`ai_profile_*`） |
| `projects[0..4]`：`name` + `business_scene` + `tech_stack` + `summary` | `career_directions` / `business_directions` / `direction_assessment` |

### 3.2 岗位侧

- `_resolve_jd_source(parsed, source_text, manual_overrides) -> (文本, 来源标签)`
  - 来源标签：`profile`（人工改过的画像）/ `source_text`（JD 原文）/ `parsed`（防御性：原文缺失时退回解析结果）
- `segment_jd_text(source_text) -> sections`：按标题行切小节，标签 ∈〔岗位职责 / 优先项 / 任职要求 / 其他〕；识别不出标题时整段归「其他」并**不加标签**（避免误标）
- `render_jd_sections(sections) -> str`：输出带【】标签的文本，让模型看得见权重
- `trim_sections(sections, max_chars=6000)`：仅超限触发。丢弃顺序：「其他」→「任职要求」自末尾往前 →「岗位职责」自末尾往前；**优先项/加分项不丢**；只在行边界截断；结果记入 `basis.trimmed`

### 3.3 搜索侧（`describe_conditions`）

- 保留：`query` 与软性字段（`direction`、`career_directions`、`career_specializations`、`business_directions`、`specializations`、`company`、`title`）
- 剔除：年限/年龄/学历/城市/QS/学校层级/学校/地区/手机/性别/姓名/排除技能

## 4. 输出契约

```json
{
  "verdict": "recommend|pending|reject",
  "project_match": ["…（projects[0]）"],
  "experience_match": ["…（experiences[1]）"],
  "tech_match": ["…（skills）"],
  "risks": ["…（experiences[0]）"],
  "basis": {
    "source": "profile|source_text|parsed|search",
    "eligible": true,
    "rejected_by": null,
    "matched_dimensions": ["projects", "skills"],
    "degraded": null,
    "trimmed": null
  },
  "failed": false,
  "error": null
}
```

- `basis`、`failed`、`error` **不进 UI**；`failed`/`error` 语义不变（失败/超时显式标记，不伪装成「无风险」）。
- 前端映射：符合点列 = `project_match` → `experience_match` → `tech_match`；风险点列 = `risks`。

## 5. 提示词（共享常量，避免平行文本）

三块常量由匹配复核与搜索复核**共用**：

- `REVIEW_WEIGHT_RULES`：岗位职责与优先项/加分项 **优先于** 任职要求（任职要求只是最低线）；职责与业务不匹配时不得推荐，即使任职要求清单全部对上；非必备项缺失不影响推荐但必须写进风险点；「缺失则无法胜任」的必备技能缺失或命中排除项 → 不推荐。
- `REVIEW_VERDICT_RULES`：三档判据（见 §6）。
- `REVIEW_OUTPUT_CONTRACT`：JSON 契约 + 「三段按顺序写、只写有依据的内容、每条一句话、结尾括号标证据编号、无证据写『未见证据』、不臆造」。

组装顺序（匹配复核）：

1. 角色：「你在复核一个岗位与候选人是否匹配。只关注技术、业务与项目层面。」
2. `REVIEW_WEIGHT_RULES`
3. `岗位要求：{渲染后的小节文本}`
4. `候选人（含项目/经历证据编号）：{candidate_text}`
5. 三点顺序说明（项目 → 工作经历 → 技术栈）
6. `REVIEW_OUTPUT_CONTRACT`

搜索复核同结构，把「岗位要求」换成「搜索条件：{describe_conditions}」。

## 6. 判定流水线与三档标准

| 步骤 | 规则 |
| --- | --- |
| 1 前置资格 | `evaluate_pair.eligibility == "rejected"` → **reject**，`basis.rejected_by` 记原因，`risks` 写明（不再标 pending） |
| 2 组装输入 | §3 的三情形 |
| 3 模型调用 | 快模型 / 深思考由 `reasoning` 开关决定；并发 5；单条 120 秒超时；失败/超时走 `_failed_verdict` |
| 4 清洗 | 三段与 risks 去空、保留原顺序；证据编号原样保留 |
| 5 一致性兜底 | 三段全空 + recommend → `pending`（`basis.degraded = "no_evidence"`）；reject 但三段均有内容 → 保留并在 `basis` 标记 |
| 6 返回 | 带 `basis` |

**三档标准（写进提示词）**

| 档位 | 判据 |
| --- | --- |
| `recommend` | 项目或工作经历有具体证据（具体系统/业务场景/本人职责）表明做过与岗位职责同类的事；技术栈与岗位技术要求对得上主干；优先项命中 ≥1 项更强；个别非必备任职要求缺失仍可推荐，但风险点必须写明 |
| `pending` | 只有技能清单对上、职责与业务场景无证据；只能靠「可能/类似」推断；项目描述太粗无法判断业务；方向尚可迁移的边界情况 |
| `reject` | 职责与业务方向明确不同且不可迁移；命中排除项或「缺失则无法胜任」的必备技能明确缺失；三个维度都没有实质匹配证据 |

## 7. 改动清单

| # | 文件 | 内容 |
| --- | --- | --- |
| 1 | `backend/src/kerui_recruit/match/review.py` | 新增 `_resolve_jd_source` / `segment_jd_text` / `render_jd_sections` / `trim_sections` / 三块共享常量 / `build_basis` / `apply_consistency_fallback`；改造 `build_review_prompt`、`build_candidate_text`（剔除项确认）、`ReviewVerdictModel`、`validated_verdict`；前置 reject 映射；删除 `build_requirement_checklist`、`RequirementVerdictModel` |
| 2 | `backend/src/kerui_recruit/search/review.py` | `build_query_review_prompt` 复用同一套常量与契约；`describe_conditions` 剔除硬筛字段；`build_basis` / 一致性兜底接入，亮点列改由三段拼接 |
| 2b | `backend/src/kerui_recruit/db/models.py`、`db/upgrades.py`、`db/migrate.py` | `search_review` 新增 `basis` JSON 列（判据依据，不进 UI）；`SCHEMA_VERSION` 18 → 19，配 `_upgrade_v18_to_v19` |
| 3 | `backend/src/kerui_recruit/api/match.py`、`api/search.py` | 无需改动：匹配复核结论经 `result_ref` 原样透传，搜索复核返回结构已含 `verdict/highlights/risks/failed`（`basis` 按要求不进 UI） |
| 4 | `desktop/src/pages/JdManagementPage.tsx` | 删除 `ReviewRequirementItem` / `reviewRequirementLines`，改 `reviewMatchLines`（三段按顺序拼接）；`ReviewVerdictItem` 换 `project_match/experience_match/tech_match/risks`；表头「注意点」改「风险点」 |
| 5 | `desktop/src/App.tsx` | 同上：符合点列按三段顺序渲染，风险点列读 `risks`，类型同步 |
| 6 | 测试 | §8 |

**明确不动**：画像生成链路（`profile_spec.py` 那套）、检索与索引、导出、排序与筛选取用 `verdict` 的逻辑、任务队列与幂等。`runtime.py` 的 handler 无需改（`jd_revision.source_text` 与 `manual_overrides` 在复核内部取用）。

## 8. 测试清单

- 小节切分：有标准标题 / 无标题（不加标签）/ 只有福利段 / 缺「优先项」/ 超长触发裁剪（断言优先项未被裁、未截半句）
- 输入分支：人工改过 → 画像；未改过 → 原文；两者都断言硬筛字段不出现
- 契约：三段 + `risks` + `basis` + `verdict` + 证据编号；`failed` 条目仍可生成
- 三档判据：三档各 1 个断言案例（清单全对但职责不符 → 不得推荐；职责匹配但必备技能缺 → 不推荐；只技能对上无业务证据 → 待核）
- 一致性兜底：三段全空 + recommend → pending
- 搜索侧：硬筛字段被剔除、软性字段保留
- 回归：导出、`sortReviewItems` 排序、取消/续租/超时、`search_review` 覆盖逻辑

## 9. 风险与边界

1. 三档标准是「提示词 + 规则」的启发式，**需要人工抽检后才算稳定**；建议先在 1 个 run 上跑 10 条核对。
2. 两个输入源信息量不同（原文中位 767 字 vs 画像 120~160 字）：画像分支更窄，**更容易给出推荐**——这是口径差异，需接受或后续统一。
3. 一致性兜底会减少推荐数量，属预期变化，可用 `basis.degraded` 观测。
4. 前端是必改项（否则「符合点」列会缺内容）。
5. 历史 20 个 JD 已失去人工标记，走原文分支；若要它们走画像分支，需从 `.tmp-jd-profile-backup/` 恢复标记。

## 10. 实施顺序

后端契约与提示词 → 单测 → 前端两列 → 端到端（快模型 + 深思考各一次）→ 灰度抽检 10 条 → 全量启用。

## 11. 实施结果（2026-09-21）

- 后端全量单测 **1317 passed / 3 skipped**（含新增的小节切分、输入源分支、三档兜底、三段契约用例）；前端 `vitest` **145 passed**、`tsc -b` 与 `vite build` 通过。
- 真实库上的只读验收（`scripts/e2e_s4s5_verify_2026_09_20.py`，假模型，23 条真实配对）：
  - 输入源分支生效：人工改过的 JD 走 `profile`（144 字），未改过的走 `source_text`；
  - 小节标注生效：一条识别出【岗位职责】【优先项】【任职要求】，另一条只识别到【岗位职责】，未从句子中间截断；
  - 契约生效：23 条全部返回三段 + `basis`，prompt 含权重规则与证据包片段；无前置资格被拒的条目。
- 前端两列表头由「注意点」改为「风险点」，与搜索侧口径一致。

