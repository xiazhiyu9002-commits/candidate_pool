# 执行文档：工作年限逐年滚动 / 简历更新只留当前版本 / 今日待办与候选人提醒

> 日期：2026-09-20
> 范围：本文覆盖四次讨论的落地实施方案，按「主题一 → 主题四」顺序推进，四个主题互不依赖，可独立交付验收。
> 前置要求：实施前必须先读「第 0 节 已定决策」与「第 9 节 不得改动项」，不得凭本文档的旧描述推翻已定决策。

---

## 0. 已定决策（实施者不得擅自更改）

| 编号 | 决策点 | 结论 | 来源 |
| --- | --- | --- | --- |
| D1 | 工作年限是否随时间变化 | 让它逐年增长（用户口径："每年加一"） | 用户确认 |
| D2 | 工作年限口径 | **连续口径**：最早一段工作 `start_date` → 当前月 | 用户确认 |
| D3 | 年限变化后画像处理 | **自动重新生成**画像 | 用户确认 |
| D4 | 工作年限改造代价 | 只做最小改动，不引入多余机制 | 用户确认 |
| D5 | "同一个人"判定口径 | **手机号 或 邮箱任一命中**即同一人（保持现状，不做 AND） | 用户确认 |
| D6 | 新简历解析失败时 | **解析成功后才删旧版**（安全网，失败不丢数据） | 用户确认 |
| D7 | "重新生成一切信息"与人工数据 | **重生成解析字段，保留人工修订**（沿用现有 `manual_fields` / `manual_overrides` 保护） | 用户确认 |
| D8 | 旧简历版本 | **只保留当前版本**，旧版本**硬删除**（物理删除，不用软删除） | 用户确认 |
| D9 | 今日待办 · 系统项勾选语义 | 勾选 = "今日已处理"，**次日重置**；不代表任务完成 | 用户确认 |
| D10 | 今日待办 · 提醒项语义 | 建好**即时**进今日待办（无日期概念）；勾选 = **任务完成**并移出列表 | 用户确认 |
| D11 | 待办项视图 | 「我的提醒」作为今日待办的**第三列**独立展示 | 用户确认 |
| D12 | 简历更新后的历史匹配结果 | **保留历史匹配记录，把 `resume_revision_id` 改指到新版本**（不删记录、不额外消耗额度） | 用户确认 |
| D13 | 画像重生的"待更新"状态 | ~~新建独立队列表~~ **已作废**（见 D15） | 用户确认 → 被 D15 取代 |
| D14 | 画像重生每日上限 | ~~20 条/日~~ **已作废**（见 D15） | 用户确认 → 被 D15 取代 |
| D15 | 画像里的年限/年龄怎么更新 | **不整段重生**：只就地替换画像文本里的「总年限」「年龄」两个数字，措辞与其他数字一律不动；人工编辑过的画像同样替换。整套"重生队列"机制已删除 | 用户确认 |
| D16 | 年龄随时间变化 | **每年 +1**；基准在首次刷新时建立、**年龄当次不变**（不补历史欠账，从本次升级年起算）；使用者手工改过年龄后基准重置为「人工值 + 当年」，之后继续逐年增长 | 用户确认 |

---

## 1. 主题一：工作年限逐年滚动 + 画像自动重生

### 1.1 现状与问题

字段名 `total_years`，全链路是**静态快照**，存在四处副本：

| 副本 | 位置 |
| --- | --- |
| 候选人列 | `Candidate.total_years`，`NUMERIC(5,1)`（`backend/src/kerui_recruit/db/models.py:39`） |
| 简历版本 JSON | `ResumeRevision.parsed_data["total_years"]`（`resumes/pipeline.py:526-527`） |
| 搜索索引列 | LanceDB `total_years`（`search/lancedb_index.py:1038`） |
| 画像文本 | 仅文本形式（画像无独立年限字段，见 1.4） |

现有产出口径是**由 LLM 估算一次**：

- 提示词：`providers/generation_tasks.py:33` 要求模型「按最早一段工作的起始年月到"至今"（当前时间）计算总年数，四舍五入到 0.5 年」。
- 无模型时兜底：`providers/local.py:31-36` 用正则从**原文文字**抠数字（`工作年限：6.5年`），连年份差都不算。
- 归一化：`resumes/normalize.py:158-162` 只做量化到 0.1，**不重算**。

**结论（已核实）**：代码中**不存在**任何随时间重算 `total_years` 的逻辑；调度器、回填、索引重建均不触碰该值。因此今年 14 年、明年仍是 14.0，不会自动变 15。

已知隐患：LLM 产出的 `total_years` 与它自己输出的 `experiences.start_date/end_date` **无代码级交叉校验**，同一份简历两次解析可能得到 14.0 / 14.5 的不一致值。

### 1.2 设计

新增**确定性的代码计算**，由每日一次的滚动刷新写入，取代"只算一次"的快照语义。

#### 1.2.1 计算函数（新增纯函数）

新建 `backend/src/kerui_recruit/resumes/tenure.py`：

```
compute_total_years(experiences: list, today: date) -> Decimal | None
  1. 遍历 experiences 的 start_date，解析为「年*12 + 月」，取最小值 S
  2. 终点 E = today.year * 12 + today.month
  3. 月数 = E - S；年限 = 月数 / 12，量化到 0.1（ROUND_HALF_UP，与 DB NUMERIC(5,1) 一致）
  4. 任一前提不成立则返回 None（= 保留原值兜底）：
     - experiences 为空
     - 所有 start_date 均无法解析
     - 解析出的 S 晚于 E（脏数据）
```

**日期解析必须复用现有实现**，不得另造一套：`direction/classifier.py:188-199` 已有 `_DATE_RE` 与 `_parse_month`（支持 `2020.07` / `2020-07` / `2020/07` / `2020年7月`）。建议把这两个符号抽到 `tenure.py`，`classifier.py` 改为从 `tenure` 导入，避免两份解析口径漂移。

注意：连续口径**只用 `start_date`**，不需要判断"至今"（`_is_present` 在这一步用不上）。

#### 1.2.2 刷新时机：挂在既有调度循环，每日一次

`SchedulerService.run_forever(interval_seconds=300)`（`scheduler/service.py:182-209`）已在跑 5 分钟轮询，并已有成熟的「每 UTC 日只跑一次」写法（`purge_soft_deleted_tick`，`scheduler/service.py:128-146` 的 `_last_purge_date`）。照抄该模式：

```
refresh_work_years_tick():
  today = 当前上海日期
  if self._last_years_refresh_date == today: return          # 每日一次，其余为 no-op
  self._last_years_refresh_date = today
  for revision in 全部 (is_current & status=READY & 候选人未删除):
      code_value = compute_total_years(parsed_data["experiences"], today)
      if code_value is None or code_value == 现有值: continue
      更新三处副本（见 1.2.3）
      收集 candidate_id
  对收集到的候选人触发画像重生（见 1.4，受上限保护）
```

在 `run_forever` 的 `operations` 列表（`scheduler/service.py:186-190`）注册 `("refresh_work_years", self.refresh_work_years_tick)`。

**日常开销接近零**：年限按 0.1 精度只在跨月时变化，界面取整后约一年变一次，因此绝大多数日次 tick 直接 `continue`。

#### 1.2.3 必须同时写三处副本

| 副本 | 写入方式 | 不写的后果 |
| --- | --- | --- |
| `Candidate.total_years` | 直接赋值 | 详情页与列表不一致 |
| `revision.parsed_data["total_years"]` | 直接赋值 | 前端列表停在旧值；且**画像输入哈希不变，画像不会重生** |
| LanceDB 索引列 | `enqueue_sync(session, "candidate", candidate_id)`（`search/sync.py:54-75`） | 出现「显示 15 年、按 15 年筛不出来」 |

**多当前版本候选人（同一候选人有多条 `is_current=True`）的处理**：必须**先按候选人聚合、每个候选人只写一次列值**（列值取创建时间最新的那条 revision 的年限），否则同一候选人被处理多次会导致列值随写入顺序抖动。此状态当前确实可能产生，成因见主题二 2.1。

#### 1.2.4 画像联动（决策 D3：自动重新生成）

**关键约束：`regenerate_candidate_profile` 不能用于自动重生**——它只生成预览、**不落库**（`backfill/service.py:151-159` docstring 明确）。

正确做法是复用批量回填的落库链路 `BackfillService._backfill_one`（`backfill/service.py:315-400`），因为它：

1. 把新画像写回 `parsed_data` 并 `enqueue_sync`（`backfill/service.py:377-399`）；
2. skip 判定依赖 `ai_profile_input_hash`（`backfill/service.py:336-341`），而 `total_years` 在画像输入字段内（`providers/profile_spec.py:152-157`）——**只要改过 `parsed_data`，哈希必然不匹配，因此只有真正变化的候选人会重生，其余自动 skip**，无需另写"筛选待更新"逻辑；
3. 已内建并发、失败统计与致命错误熔断（`_FATAL_BACKFILL_CODES`）；
4. **会跳过人工画像**（`manual_overrides` 命中或 `ai_profile_source == "manual"`，`backfill/service.py:333 / 370`），必须保留该保护。

需新增一个**按 revision id 列表回填**的公开入口（现有 `backfill_candidate_profiles` 是全库扫描、无 id 过滤参数），内部仍复用 `_backfill_one`，供 tick 只处理本次变化的少量候选人。

#### 1.2.5 待重生队列（决策 D13，替代"顺延"）

> **本节已作废**：D13/D14 被 D15 取代——画像不再整段重生，整套队列机制已删除，
> 改为就地替换画像里的年限/年龄数字。**以第 12 节为准**。本节保留作为设计沿革记录。

**必须纠正一个会导致丢任务的写法**：不能"每天取前 20 条交给回填、其余顺延到次日"——因为收集条件是"本次运行中 `total_years` 发生变化"，被顺延的候选人次日年限已经写好、不再"变化"，**永远不会被再次收集**，画像将永久停在旧年限。这不是顺延，是丢任务。

正确做法是把"待重生"状态**持久化到独立队列表**：

```
work_years_regen_queue
  id / revision_id VARCHAR(36) UNIQUE / candidate_id VARCHAR(36) / created_at
```

- 年限变化且**取整后确实变化**（`int(旧) != int(新)`）时，把该候选人的最新 revision 入队（`revision_id` 唯一约束天然去重）。
- 每日 tick 从队列取最多 **20 条**（D14）交给 `backfill_candidate_revisions`，**处理成功即出队**；未处理的留在队里，次日继续 —— 不丢任务。
- **刻意不复用 `ai_profile_stale`**：复用会让这批候选人在界面显示"画像待更新"，并因 `_backfill_one` 的跳过判定是"哈希一致**且**未 stale"而**连带改变手动画像回填的处理范围**，违反"不影响其他任何功能"的约束。
- 清理：候选人被删除或 revision 不再 `is_current & READY` 时，对应队列行应一并清除。

**不影响的功能（需在验收中显式确认）**：

- **JD 画像回填完全不受影响**——工作年限是候选人独有字段（JD 侧只有表示岗位要求的 `min_years`），滚动刷新只扫 `ResumeRevision`，不触碰 `JdRevision`。
- **候选人画像回填（手动/前端触发的全库批量）行为不变**——全库扫描链路不加任何上限；队列表只服务自动路径。

### 1.3 改动清单

| 文件 | 改动 |
| --- | --- |
| `backend/src/kerui_recruit/resumes/tenure.py` | 新增：`compute_total_years` + 共享日期解析 |
| `backend/src/kerui_recruit/resumes/work_years_rollover.py` | 新增：滚动刷新器（三处副本同步 + 待重生队列出入队） |
| `backend/src/kerui_recruit/db/models.py` | 新增 ORM 模型 `WorkYearsRegenQueue` |
| `backend/src/kerui_recruit/direction/classifier.py` | 改为从 `tenure` 导入 `parse_month` |
| `backend/src/kerui_recruit/scheduler/service.py` | 新增 `refresh_work_years_tick()`、`_last_years_refresh_date`、在 `operations` 注册 |
| `backend/src/kerui_recruit/backfill/service.py` | 新增「按 revision id 列表回填」公开入口，复用 `_backfill_one` |
| `backend/src/kerui_recruit/runtime.py` | 把回填服务引用注入调度器 |
| `desktop/src/...` | 可选：候选人详情显示「年限按 YYYY-MM 计算」的基准时间 |

**不修改**解析落库路径（LLM 提示词 `generation_tasks.py:33` 保持不变，仍作兜底），因此 `tests/resumes/test_pipeline.py`（断言 `5.0`）、`tests/resumes/test_normalize.py`、`tests/providers/test_local.py` 均不回归。

### 1.4 护栏（必须实现）

1. **首日成本**：第一次运行时代码值会与现有 LLM 估值普遍存在小幅差异（如 LLM 给 14.0、代码算 14.3），会一次性判定一大批候选人"年限变化"并触发画像重生。
   **策略**：只在**取整后确实变化**（`int(旧) != int(新)`）的候选人上入队重生；队列每日最多出队 20 条（D14），其余留在队列次日继续（见 1.2.5）。
2. **连续口径的固有偏差**：候选人若已离职多年（如 2010–2015 工作后未就业），连续口径仍会算出 15 年。这是 D2 的既定代价，不修正。
3. **人工画像不被覆盖**：改过画像的候选人会被 `_backfill_one` 跳过，其画像文本里的「X年」会与新数值不一致，需在界面标注。

---

## 2. 主题二：同一人更新简历，只保留当前版本

### 2.1 现状与根本障碍

**决策 D5 已与现状一致**：`resumes/identity.py:33-88` 的 `resolve_identity` 就是「手机号或邮箱任一命中 → MATCHED」，姓名只产生 `POSSIBLE_DUPLICATE`（不自动合并）。**主题二不需要改动判定口径**。

真正的障碍在时序：身份判定发生在**解析之后**（`resumes/pipeline.py:196`）。导入那一刻系统：

1. `_resolve_candidate`（`resumes/ingest.py:205-216`）——`candidate_id` 为空 → **必然新建占位候选人**；
2. `_resolve_document`（`resumes/ingest.py:218-230`）——新候选人无文档 → **必然新建文档**；
3. `resumes/ingest.py:100-104` 那条 `update(ResumeRevision).where(document_id == 新文档).values(is_current=False)` —— **影响 0 行**（新文档下没有 revision）。

**已核实的两个结论**：

- 三个生产调用方（`api/resumes.py:90`、`api/resumes.py:124-127`、`mail/ingest.py:92`）**全都不传 `candidate_id`**；`IngestResult.action` 中声明的 `UPDATED` **在全库没有任何一处产生**。因此"复用文档并清空旧版本"这段逻辑在生产中**是死代码**。
- 生产产生「同一候选人多条 `is_current=True`」的唯一路径是 `pipeline._resolve_identity`（`resumes/pipeline.py:441-453`）：它把新 revision 的 `document_id` 改挂到目标文档，**但不清理目标文档已有 revision 的 `is_current`**；`db/models.py:109` 的 `ix_resume_revision_current` 只是普通索引、非唯一约束，无 DB 层兜底。

**决策**：**不把身份判定前移到导入阶段**（前移需要先做一遍文本正则粗抽，会引入第二套识别口径、易与精确识别漂移），而是**保留"解析后合并"机制，在合并成功的瞬间删除旧版本**。这天然满足 D6（解析成功后才删）。

### 2.2 新的合并时序

在 `resumes/pipeline.py:_resolve_identity`（`resumes/pipeline.py:401-473`）的 MATCHED 分支内扩展：

```
导入 → 新建占位候选人 C2 + 文档 D2 + 版本 R2(PENDING)
  → 解析 R2
      失败 ⇒ 不做任何删除（安全网，D6），保留现状并走既有失败处理
      成功 ⇒ resolve_identity
           命中 C1 ⇒ ① 迁移人工修订（见 2.3）
                     ② R2 改挂到 D1
                     ③ 硬删 D1 下除 R2 外的全部旧版本（见 2.4）
                     ④ 硬删占位候选人 C2（不再用 deleted_at）
                     ⑤ 重建 C1 的解析信息（见 2.5）
           未命中 ⇒ 维持现状（新建候选人，与今天行为一致）
```

硬删占位候选人可**轻量处理**：此时 D2 已空并被删除、`CandidateContact` 建在目标候选人上、`ResumeImportClaim` 已改指 target，因此直接 `session.delete(candidate)` 即可（靠 relationship cascade 清理残留）。

### 2.3 人工修订迁移（最高风险步骤，必须有独立测试）

`manual_overrides` 是**逐 revision 存储**的（`db/models.py:125`），硬删旧版本前必须：

1. 把旧 revision 的 `manual_overrides` 合并进 R2 的 `manual_overrides`；
2. **对 R2 的解析结果重放这些覆盖**——复用 `resumes/pipeline.py:189-192` 已有的覆盖应用顺序（`ParsedResume.model_validate({**parsed, **overrides})`），否则人工值只存在于 overrides 字段里、不体现在 `parsed_data` 中；
3. 注意与 `_persist_ready` 的冲突校验对齐：`resumes/pipeline.py:491-494` 会在 `revision.manual_overrides != expected_overrides` 时抛 `E_REPARSE_CONFLICT`，**合并后的集合必须作为该校验的基准**。

`CandidateContact.manual_fields` 与 `confidence` 是**候选人级**的（`db/models.py:59-64`，`candidate_id` 唯一），重导入不会因删 revision 而丢失，天然满足 D7 中"保留人工维护的手机号/邮箱"。

### 2.4 硬删单条 revision 的六步收尾

**现状：全库没有任何删除单条 `ResumeRevision` 的代码**（`grep "delete(ResumeRevision)"` 零命中）。新增 `backend/src/kerui_recruit/resumes/revision_purge.py`，对每条旧版本依次执行：

| 步骤 | 动作 | 依据/原因 |
| --- | --- | --- |
| 1 | 迁移 `manual_overrides` 到新版本 | 见 2.3 |
| 2 | `Blob.reference_count = max(0, rc - 1)`；归零则删 blob 行并登记 `BLOB_CLEANUP` | 照抄 `resumes/deletion.py:108-110, 123-128`。不减计数会导致 blob 行与磁盘文件**双重泄漏**；同内容文件共用同一 blob，不得归零 |
| 3 | `index.delete_revision(old_revision_id)` | `search/lancedb_index.py:490-495`。不删会留下孤儿 chunk 长期占用召回池 |
| 4 | 把指向被删 revision 的 `ResumeImportClaim` **改指 R2** | **最危险的一条**：claim 是裸字符串无外键（`db/models.py:144`），悬空后再次导入同一文件会命中 `ingest.py:64-76` 短路，返回 `ALREADY_IMPORTED` + **一个已死的 revision_id** |
| 5 | 取消 payload 指向被删 revision 的未终态 `TaskRecord` | 否则 worker 领到任务后在 `resumes/pipeline.py:133-134` 抛 `LookupError`，重试到死信 |
| 6 | 处理 `MatchResult.resume_revision_id` / `SearchReview.revision_id` 悬空 | 两者均为裸字符串无外键（`db/models.py:519 / 543`），不会报错但会静默失效（`api/match.py:344-349`、`match/review.py:440-441`）。按 D12 删除该候选人旧匹配结果并重触发一次匹配 |

SQLite 侧**没有任何表以 `resume_revision.id` 为外键**，因而不存在外键失败风险（`PRAGMA foreign_keys=ON` 已在 `db/session.py:28` 开启）。

### 2.5 重建"一切解析信息"（决策 D7：重生成解析字段、保留人工修订）

| 数据 | 处理 |
| --- | --- |
| `display_name` / `total_years` / `highest_degree` | 由 `_persist_ready` 自动覆盖（`resumes/pipeline.py:495-498` 已是此行为） |
| 手机 / 邮箱 | 沿用现有保护：`manual_fields` 内或 `confidence == 1.0` 的不覆盖（`resumes/pipeline.py:510-524`）✅ 符合 D7 |
| AI 画像 | 解析期同一次调用产出；**额外**调用一次画像回填确保重建（跳过 `source=manual`） |
| 职业方向 | 复用 `BACKFILL_DIRECTION`（`direction/backfill.py:96-120`，对人工修订跳过） |
| 学校等级 / QS | 复用 `schools/backfill.py:88-107`（尊重 overrides） |
| 搜索索引 | `index.delete_revision(旧)` + `enqueue_sync(session, "candidate", c1)` 整体重建 |
| 历史匹配结果 | 按 D12 删除并重新触发一次反向匹配 |

### 2.6 不需要改的部分

`search/sync.py:_snapshot`（`search/sync.py:342-352`）的多版本合并逻辑**不修改**：在"每人只有一份 current revision"的新不变量下，它的循环只会跑一次；保留为**防御性代码**，可显著减小改动面。

### 2.7 改动清单

| 文件 | 改动 |
| --- | --- |
| `resumes/revision_purge.py` | 新增：单条 revision 硬删的六步收尾 |
| `resumes/pipeline.py` | `_resolve_identity` MATCHED 分支：迁移人工修订 → 硬删旧版 → 硬删占位候选人；落库后触发画像/方向/匹配重建 |
| `resumes/ingest.py` | 占位候选人改为可硬删；确认 claim 改指顺序 |
| `backfill/service.py` | 复用（新增按 id 回填入口，与主题一共用） |
| 测试 | 见第 8 节 |

---

## 3. 主题三：今日待办勾选（系统项）

### 3.1 现状与障碍

**「今日待办」不是 reminders，而是 `daily_followup`**：

- 前端：`desktop/src/pages/DashboardPage.tsx:191-224` 渲染标题「今日待办」的卡片，左「追反馈」右「待面试」，**纯只读、无任何按钮或 checkbox**。
- 数据：`GET /api/daily-followup/today`（`api/daily_followup.py:14-52`），由 `DailyFollowupService.gather`（`daily_followup/service.py:83-154`）**实时计算、不落任何表**；只有邮件发送状态落 `daily_followup_state`。
- `api/daily_followup.py:26-50` 只透出 `name / company / title / date|time` —— **待办项没有任何稳定 ID**，无法直接实现"勾选"。

### 3.2 设计

**稳定键**：在 `gather()` 内为每项带上 `case_id`（循环里已有 `case` 对象），令

```
item_key = "{category}:{case_id}"     category ∈ followup | interview
```

由于 `gather` 的 `if / elif` 结构保证一个 case 只落入四类桶中的一类，而 API 把「追反馈」映射为 `recommended_no_feedback + interview_no_feedback`、「待面试」映射为 `today_interview + tomorrow_interview`，因此一个 case 只会属于 followup 或 interview 之一，`item_key` 天然唯一。

**新增表** `daily_todo_check`：

```
id / check_date VARCHAR(10)   -- 上海日期 YYYY-MM-DD
   / item_key   VARCHAR(120)
   / checked_at DATETIME
UNIQUE (check_date, item_key)
```

**每日重置（D9）是免费的**：勾选记录带 `check_date`，次日按当天日期查询自然查不到 → 回到未勾选，**无需任何定时任务**。

**API**：

- `GET /api/daily-followup/today` 返回项增加 `item_key` 与 `done`（按今天查询 `daily_todo_check`）
- 新增 `POST /api/daily-followup/check`，请求体 `{item_key, done}` → 按（今天, item_key）插入或删除

**前端** `desktop/src/pages/DashboardPage.tsx:198-221`：每行加 checkbox，勾选后加样式类。**需新增** CSS 类（`desktop/src/styles.css` 现无可用组合类，只有 `.muted`（行 118）和绑在条件标签上的 `.condition-tag.is-removed`（行 960））：

```css
.is-done { color: #9ca3af; text-decoration: line-through; }
```

**残留清理**：case 进入终态或候选人被删后待办项消失，其历史勾选记录无害；可在既有每日清理里顺带删除超过 N 天的记录（可选，非必须）。

---

## 4. 主题四：候选人提醒（今日待办第三列）

### 4.1 为什么新建表而不用现有 `reminder`

现有 `reminder` 表（`db/models.py:656-669`）不适合直接扩展，三条理由均已核实：

1. `remind_at` **非空**，而候选人提醒是**无日期、即时进待办**（D10）；
2. 它有 `paused_by_workflow`，会被**流程终态自动暂停**（`cases/service.py:751-755`）——个人提醒不该受流程状态影响；
3. 它会被 `ReminderMailService` **到期发邮件并自动 dismiss**（`reminders/mail_service.py:24-36`）——候选人提醒不该触发邮件。

此外 `Reminder` 表**没有 `candidate_id` 字段**（只有 `case_id`），要做"固定显示人名"无论如何都要改结构。

### 4.2 新表设计

```
candidate_reminder
  id
  candidate_id            VARCHAR(36) FK → candidate.id
  candidate_name_snapshot VARCHAR(200)     -- 固定显示人名（改名/删除后不变）
  content                 VARCHAR(500)     -- 使用者写入的内容
  done                    BOOLEAN NOT NULL DEFAULT 0
  done_at                 DATETIME
  created_at / updated_at

INDEX (candidate_id, done)
```

- `candidate_name_snapshot` 用于满足"**固定显示人名**"：候选人改名或删除后，提醒行仍显示建立时的人名。
- `done` 即 D10 的"勾选 = 完成了这个任务"，完成后移出今日待办（记录保留可追溯）。

### 4.3 接口与前端

| 位置 | 改动 |
| --- | --- |
| 后端 | `POST /api/candidates/{candidate_id}/reminders`（body: `content`）；`GET /api/candidate-reminders?open=true`；`POST /api/candidate-reminders/{id}/done` |
| `desktop/src/components/CandidateTable.tsx` | 行内操作区（第 564-567 行 `<span className="row-actions__group">`）增加「建提醒」按钮，与「匹配 / 查看详情 / 建流程」并列；`CandidateTableProps`（第 336-360 行）新增 `onCreateReminder(candidateId, name)` 回调 |
| `desktop/src/pages/TalentPoolPage.tsx` | 两处 `<CandidateTable>`（第 266-301、303-338 行）接线新回调 |
| `desktop/src/App.tsx` | 提供「建提醒」弹窗（只输入内容，人名自动带入并写入快照） |
| `desktop/src/pages/DashboardPage.tsx` | 今日待办增加**第三列「我的提醒」**，行内容 = `人名 + 内容`，checkbox 勾选 = 完成并移出 |

> 决策 D11：提醒放**第三列**而非混入「追反馈」。理由：两类项的勾选语义不同（D9 vs D10），混排会让使用者分不清"打过勾到底算不算完成"。

---

## 5. 数据库迁移

- **`SCHEMA_VERSION` 20 → 21**（`db/migrate.py:17`）。
- 新增三张表（`work_years_regen_queue`、`daily_todo_check`、`candidate_reminder`）：作为 ORM 模型由迁移内的 `Base.metadata.create_all(connection)`（`db/migrate.py:66`）自动建出。
- **仍需注册一条对应的 `Upgrade`**：即使 DDL 为空操作，`db/migrate.py:68-72` 的版本循环与 `tests/db/test_migrate.py::test_schema_version_matches_last_upgrade` 都要求每个版本必须有升级步骤。
- 升级前自动快照 `.pre-v20-to-v21-*.sqlite3`（`db/migrate.py:87-103`）已有保障，无需新增。

---

## 6. 分期与交付顺序

| 期 | 内容 | 依赖 |
| --- | --- | --- |
| 一期 | 主题一：工作年限逐年滚动 + 画像自动重生 | 无 |
| 二期 | 主题二：简历更新只留当前版本 | 无（与一期共用「按 id 回填」入口，建议紧随一期） |
| 三期 | 主题三：今日待办勾选 | 无 |
| 四期 | 主题四：候选人提醒 + 第三列 | 与三期共用 DashboardPage 与样式类，建议紧随三期 |

每期独立验收，不得把四期合并成一次提交。

---

## 7. 建议的实现顺序（TDD）

严格按「先写失败测试 → 修根因」推进，每完成一步立即跑测试，不得最后一次性补测。

**一期**
1. 写 `tenure.compute_total_years` 的失败测试（常规段 / 缺 `start_date` / 未来日期 / 空 experiences / 跨年边界，`today` 必须可注入以保证可复现）→ 实现
2. 写 tick 幂等测试（同日第二次 no-op；值未变时**不写库、不 enqueue**）→ 实现
3. 写三处副本同步测试 → 实现
4. 写画像联动测试（`parsed_data` 变化 → 判定需重生；`source=manual` / `manual_overrides` 命中 → 跳过）→ 实现
5. 写护栏测试（单次 tick 重生条数不超上限）

**二期**
6. 写硬删测试：旧 revision 消失、Blob 计数正确、**claim 改指新版本（断言再次导入同一文件不会返回已死 revision_id）**、索引无孤儿、任务被取消
7. 写人工修订保留测试：改过手机号/字段后重新导入 → 人工值仍在，解析字段已更新
8. 写解析失败安全网测试：新版解析失败 → 旧简历仍在、候选人仍可用
9. 写"每人只剩一份 current revision"不变量测试
10. 实现 `revision_purge` 与 `_resolve_identity` 扩展

**三期**
11. 写按天隔离测试（同日勾选生效、次日重置）、`item_key` 稳定性测试 → 实现

**四期**
12. 写提醒测试：建立后进今日待办、勾选后移出、候选人改名/删除后仍显示快照人名 → 实现

---

## 8. 验证与验收

### 8.1 命令

```
# 后端
cd backend
py -3.12 -m pytest tests/resumes tests/scheduler tests/backfill tests/search tests/api -q
py -3.12 -m pytest -q --ignore=tests/soft_delete --ignore=tests/search/test_sync.py

# 前端
cd desktop
npx tsc -b --noEmit
npx vitest run
npx playwright test tests/ai-settings.spec.ts    # 默认端口被占用时用 KERUI_E2E_BACKEND_PORT / KERUI_E2E_FRONTEND_PORT
```

**验收标准**：相对实施前基线**不得新增失败**。若存在实施前即可复现的时间敏感失败，验收报告必须列出前后同名用例与复现次数，不得宣称"全绿"。

### 8.2 受影响的现有测试（必须逐一确认，不得默默改断言）

| 测试 | 影响 | 处理 |
| --- | --- | --- |
| `tests/search/test_sync_unified_view.py` | 手工构造"一个候选人两条 current revision"。因**不改** `_snapshot`，**仍会通过** | 保留，并加注释说明"仅防御性，生产路径不再产生该状态" |
| `tests/api/test_search_consistency.py:130-140` | 同上，构造两条 current revision | 同上；若同时收紧 `_hydrate_hits` 则需重写 |
| `tests/resumes/test_ingest.py:26-48` | 带 `candidate_id` 导入，是唯一能产生同候选人第二版本的入口 | 需确认改造后语义并更新 |
| `tests/resumes/test_ingest_idempotency.py:30-53` | 同文件导两次 → 1 candidate / 1 revision | 走 `ALREADY_IMPORTED` 短路，预计仍通过 |
| `tests/resumes/test_deletion.py` | 候选人级删除 | 不受影响（新增的是 revision 级删除） |
| `tests/providers/test_local.py`、`tests/resumes/test_normalize.py`、`tests/resumes/test_pipeline.py` | 一期不改解析落库路径 | 预计不回归 |

**严禁**为了让测试通过而修改既有断言所表达的意图（本仓库历史上有过"测试迁就实现"导致约束失效的先例，见第 9 节）。

---

## 9. 不得改动项 / 历史教训

### 9.1 受保护项（不得改动）

- `INDEX_SCHEMA_VERSION = "8"`、`INDEX_CHUNK_VERSION = "5"`
- `query_plan`、关键词 smart/AND/OR、向量/混合语义改写、`direction` 字段
- `REPARSE_FAILED`、`E_PARSE_INCOMPLETE` 与既有视觉重解析流程
- Embedding / Rerank 仍走 SiliconFlow 与本地，不并入本次改动
- `jieba` data/submodule 收集（打包脚本）

### 9.2 历史教训（不得重演）

1. **测试迁就实现**：`kimi_code` 的 `interactive_only` 约束曾在设计文档里写明，但因实现未落地、且 `tests/providers/ai/test_catalog.py:52` 被改成断言现网值，导致约束**从未真正生效**。本次所有"约束类"改动必须**有断言真正生效的测试**，禁止改断言去迎合实现。
2. **文档与实现不一致**：改动落地后必须同步更新 `使用说明.md` 与 `docs/verification/`，不得只改代码。
3. **裸字符串引用悬空**：`ResumeImportClaim.revision_id`、`MatchResult.resume_revision_id`、`SearchReview.revision_id` 都是无外键的裸字符串，删数据前必须显式处理引用（主题二 2.4 第 4-6 步）。

---

## 10. 决策补记

原「待确认事项」已全部拍板并上移至第 0 节（D11 / D12 / D13 / D14）。本节保留两条实施时的注意事项：

1. **D12 的语义代价**：把 `MatchResult.resume_revision_id` 改指新版本后，该记录展示的仍是按**旧简历**算出的历史分数，而 AI 深度复核取到的是**新简历**。这是"保留历史记录 + 不消耗额度"的必然折中；如需结果与新简历完全对应，应改为"删除并重算"（会消耗模型额度）。
2. **首日画像重生的实际耗时**：队列按 20 条/日消化，若首次口径校正命中数百人，全部重生完毕需要十余天。这是 D14 的既定代价；如需加速，可临时调大 `profile_regen_limit`。

---

## 11. 实施记录与偏差（2026-09-20 已完成）

四期全部实施完毕，验证结果见 11.3。以下是**与本文档原设计不一致的地方**，均已在代码注释中说明原因。

### 11.1 相对原设计的偏差

| 项 | 原设计 | 实际实现 | 原因 |
| --- | --- | --- | --- |
| 主题二 2.5「额外调用一次画像回填」 | 合并后额外跑一次画像回填 | **不调用** | 解析期已在同一次调用里产出画像，额外回填会被输入哈希判定为 skip（纯空跑）；列字段/画像/方向/学校/索引均在解析落库路径内重建，已覆盖"重新生成一切信息" |
| 主题四 4.3 端点 | `POST /api/candidates/{id}/reminders` + `GET /api/candidate-reminders` + `POST /{id}/done` | `POST /api/candidate-reminders`（body 带 candidate_id）+ `POST /api/candidate-reminders/{id}/done` | 单一前缀更简单；**去掉未使用的 GET 列表端点**——「今日待办」第三列本身就是开放提醒的列表视图 |
| 主题三「今日待办」第三列数据来源 | 未写明 | `GET /api/daily-followup/today` 响应新增 `reminders` 字段 | 三列同属一张卡片，一次请求取齐；避免前端并行拉两个接口 |

### 11.2 实施中发现的三个真实缺陷（已修）

1. **`_resolve_identity` 的返回值语义**：最初未命中时返回空 dict，会把本版本自己的人工覆盖清空，导致 `_persist_ready` 的 `E_REPARSE_CONFLICT` 冲突校验**误报**。改为返回 `(candidate_id, merged_overrides | None)`，`None` 表示"无需变更、保持原样"。由 `tests/resumes/test_review_regressions.py` 捕出。
2. **版本级删除的顺序依赖**：先删 `Blob` 会让仍引用它的旧版本在 flush 时被置空 `blob_id`（NOT NULL）而报错。必须**先删版本、再删 Blob**。
3. **队列表的出队判定**：只有整批都拿到终态（updated/skipped）才出队，否则整批保留次日重试；已成功条目次日会被**纯本地哈希**判定为 skip，不产生额外模型调用。

### 11.3 验证结果

```
backend  py -3.12 -m pytest -q --ignore=tests/soft_delete --ignore=tests/search/test_sync.py
         → 1561 passed, 3 skipped, 1 deselected, 0 failed
desktop  npx tsc -b                                  → exit 0
         npx vitest run                              → 178 passed (16 files)
         npx playwright test tests/ai-settings.spec.ts → 2 passed
```

**注意**：`npx tsc -b --noEmit` 与 `-b` 组合会走增量缓存、漏报类型错误（本次打包时被
`npm run build` 的 `tsc -b` 抓出 `tests/CandidateReminderDialog.test.tsx` 的类型过窄）。
**准入门槛必须用 `npx tsc -b`**，不要用 `--noEmit`。

新增测试文件：`tests/resumes/test_tenure.py`、`tests/resumes/test_work_years_rollover.py`、
`tests/resumes/test_revision_purge.py`、`tests/reminders/test_daily_todo_check.py`、
`tests/reminders/test_candidate_reminders.py`、`desktop/tests/DashboardTodo.test.tsx`、
`desktop/tests/CandidateReminderDialog.test.tsx`。

### 11.4 未做（明确未纳入）

- 未把 `search/sync.py::_snapshot` 的多版本合并逻辑删除：新不变量下它只循环一次，保留为防御性代码（见 2.6）。
- 未对 `ai_profile_stale` 做任何联动：刻意用独立队列表隔离，避免影响手动画像回填的范围与界面状态（见 1.2.5）。

### 11.5 补充：滚动刷新改为分批提交（首日锁风险）

首日口径校正会命中大量候选人（实测：45 位候选人中 44 位数值有变化、33 位取整后变化）。
原先的实现把「全部更新 + 索引入队 + 队列写入」放在**一个事务**里，会长时间持有 SQLite
写锁；`busy_timeout` 只有 5 秒，并发的前端写入会被打成 `database is locked`。

现改为：**先做一次轻量读（只取 revision id 与 candidate_id，不加载 `parsed_data`、不开写事务），
再按 `DEFAULT_COMMIT_BATCH_SIZE = 200` 分批、每批一个独立短事务**。重算是幂等的，某一批
失败后重跑即可。分批后仍保持「同一候选人若有多条当前版本，更新的那条赢」——依赖外层按
`created_at` 升序切批、批内也按升序处理。

### 11.6 打包与覆盖安装记录（2026-09-23）

```
sidecar   powershell -ExecutionPolicy Bypass -File backend/packaging/build_sidecar.ps1
          → desktop\src-tauri\binaries\kerui-recruit-sidecar.exe\（onedir，含 _internal）
安装包    cd desktop; npm run tauri:build:windows
          → desktop\src-tauri\target\release\bundle\nsis\recruit_0.1.0_x64-setup.exe（126.6 MB）
安装      静默覆盖安装（当前用户级，无需提权）：setup.exe /S → 退出码 0
```

**安装前必须确认**（本次已确认）：Windows 版是**便携式**——数据目录是 `<安装目录>\data`
（实测 `E:\recruit\data`，含 `db`/`blobs`/`search`/`config`，48.6 MB）。已核对生成的
`target\release\nsis\x64\installer.nsi`：卸载段是**逐文件清单式 `Delete`，全文无 `RMDir`**，
因此 `data\` 不会被升级流程删除。仍然在安装前做了一份完整备份到
`E:\recruit-data-backup-<时间戳>\`。

**覆盖安装后的验证**：

| 项 | 结果 |
| --- | --- |
| 已安装 sidecar 与构建产物 | 字节数一致（20,601,833），时间戳一致 |
| 生产数据库完整性 | `recruit.sqlite3` 的 SHA256 与安装前备份**完全一致** |
| 冻结包含新模块 | 用真实数据的**副本**启动已安装的 sidecar → `GET /api/daily-followup/today` 返回 200，且响应含 `reminders` 字段（真实数据返回 2 条追反馈） |
| 迁移 | `user_version` 已到 21；`work_years_regen_queue` / `daily_todo_check` / `candidate_reminder` 三表全部建出 |
| 数据不变量 | 45 位候选人 / 45 条当前版本（"每人只剩一份当前版本"成立） |

**真实数据下的口径校正结果**（抽查 5 位逐条核对履历时间线）：

- 吴煜晗：最早 2024.06 → 正确值 2.3，旧值 4.0（旧值**高估 1.7 年**）
- 周潇：最早 2016.07 → 正确值 10.2，旧值 9.5（旧值低估）
- 孟龄：最早 2018.07 → 正确值 8.2，旧值 11.0（旧值**高估 2.8 年**）
- 陆芸仙：最早 2014.07 → 正确值 12.2，旧值 11.5（旧值低估）
- 高臻：最早 2016.07 → 正确值 10.2，旧值 8.5（旧值低估）

**结论**：新口径在全部抽查样例上都正确，旧值（模型按 0.5 估）偏差最大到 2.8 年。首次
校正后列表里的年限会明显变动，这是**修正**而不是故障。45 位候选人产生 33 条待重生，
按 20 条/日约 **2 天**清空（此前预估"十余天"是按数百人的库估的；库越大越久，500 人约 18 天）。

---

## 12. 变更：画像改为"就地替换数字"、年龄纳入滚动（取代 D13/D14）

> 本节取代 1.2.4 的"画像自动重生"、1.2.5 的"待重生队列"与 11.5 里"每日 20 条"的部分。

### 12.1 为什么改

用户要求：**年龄、工作年限、以及 AI 画像里的工作年限都要随时间变化**，并且**画像不需要
整段改变——只改那一个数字**，人工改过的也照样随年份变。

原方案（年限取整变化 → 调模型整段重生画像）有三个问题：要花模型额度、首日会命中一大批
候选人需要十余天消化、且会覆盖使用者精心改过的措辞。就地替换则**零额度、零等待、不改措辞**。

### 12.2 新设计

| 字段 | 规则 |
| --- | --- |
| 工作年限 | 不变：最早一段工作的起始月 → 当前月（连续口径），每天重算 |
| 年龄 | `基准年龄 + (今年 − 基准年)`。**基准在首次刷新时建立，年龄当次不变**（不补历史欠账，从本次升级年起算）。使用者手工改年龄后，基准重置为「人工值 + 当年」（`api/resumes.py` 的 `update_candidate_parsed`），之后继续逐年增长 |
| 画像文本 | 只替换「总年限」与「年龄」两个数字；**其余文字与其他数字一律不动** |

画像替换的两条规则（`resumes/profile_text.py`）：

1. 优先匹配「**N年…经验**」——画像规范要求的总年限标准写法；
2. 找不到该句式时，**全文只有一个「N年」且其数值等于本次重算前的年限**才替换它
   （历史画像里也有只写「4年」不带「经验」的）。`previous_total_years` 必须传入：画像里的
   数字是上一轮写进去的，拿新值去比是比不上的。

**刻意不碰**：`2024年` 这类年份；「近4年先后任职于…」「3年架构经验」这类单段经历 /
子技能年限。实测数据里这确实会命中（VIKKI 的画像同时有「约10年…经验」与「近4年先后任职」）。

覆盖双形态的三个字段（`ai_profile_summary` / `ai_profile_narrative` / `ai_profile_points`），
避免同一份画像里新旧数字并存。实测当前库里后两个字段为 `None`，只有整体段落。

### 12.3 删除的东西

`work_years_regen_queue` 表（`SCHEMA_VERSION` 21 → 22 时 `DROP TABLE IF EXISTS`）、
`WorkYearsRollover` 的队列逻辑、`BackfillService.backfill_candidate_revisions`、
`WORK_YEARS_PROFILE_REGEN` 任务类型与其 runtime 处理器、调度器里的入队与 `task_repository` 依赖。

### 12.4 顺带查证的两件事（决定成本）

- **年龄不进向量文本**（只进关键词文本与索引元数据列 `age`，该列参与 `min_age`/`max_age`
  筛选）→ 改年龄**不产生 embedding 调用**，但必须 `enqueue_sync` 否则年龄筛选会用旧值。
- **年限进向量文本**（v8 的 `_readable_years`）→ 改年限会改变 `vector_text`；索引同步的
  向量缓存是**按 `vector_text` 分键**的（`search/sync.py` 的 `by_text`），新文本不在缓存里
  会重新嵌入，因此不会出现"文本变了、向量还是旧的"的静默退化。代价是每位变化候选人
  每年一次嵌入调用。

### 12.5 验证

```
backend  py -3.12 -m pytest -q --ignore=tests/soft_delete --ignore=tests/search/test_sync.py
         → 1568 passed, 3 skipped, 1 deselected
         （唯一失败 tests/search/test_consistency.py::test_evidence_read_obeys_same_deadline
           是既有的时间敏感用例，单独运行 0.25s 通过，与本次改动无关）
```

新增测试：`tests/resumes/test_profile_text.py`（画像替换的 10 条边界）、
`tests/api/test_candidate_age_rollover.py`（人工改年龄重置基准）；
`tests/resumes/test_work_years_rollover.py` 重写（去队列、加年龄 5 条 + 画像 3 条）。

### 12.6 打包与覆盖安装（第二轮，2026-09-23）

```
安装包  desktop\src-tauri\target\release\bundle\nsis\recruit_0.1.0_x64-setup.exe（126.7 MB）
安装    静默覆盖安装（当前用户级）setup.exe /S → 退出码 0
备份    安装前完整备份到 E:\recruit-data-backup-20260923-202740\
```

安装后用**真实数据的副本**运行**已安装的** sidecar 验收（不碰生产库）：

| 项 | 结果 |
| --- | --- |
| 服务可用 | `GET /api/daily-followup/today` → 200 |
| 生产数据库完整性 | `recruit.sqlite3` 的 SHA256 与安装前**完全一致** |
| 已安装 sidecar 与构建产物 | 字节数一致（20,602,444） |
| 迁移 | `schema_version` 为**历史表**（每版本一行，读当前版本要取 `max(version)`）：副本升到 **v22**，生产仍停在 v17（未启动过）。**注意**：v21 会先建 `work_years_regen_queue`、v22 再删掉，副本最终无该表，说明两段迁移都正确执行 |
| 新表 | `daily_todo_check` / `candidate_reminder` 建出；`work_years_regen_queue` 不存在 |
| 年龄基准 | **44/44 建立基准，基准年 2026，年龄变化 0 人**（符合"首次只建基准、不补历史欠账"） |
| 画像替换 | 35/44 画像被替换数字；**画像里的 4 位年份被误改 0 例**（关键安全断言） |

实测替换样例：罗亮「4年后端开发经验」→「5年…」；周潇「约9.5年工作经验」→「约10年」且
「其中7年…、3年…」未动；吴煜晗「总工作年限约4年」→「约2年」且「2024年6月」未动；
徐猛「约11.5年」→「约12年」且「1992年生」「2014届」未动。

**离线核对到的迁移版本读取坑**（写在这里防止后续误判）：`schema_version` 不是单行版本表，
而是**升级历史表**（`PRIMARY KEY (version)`，每次升级插一行）。用 `fetchone()` 读到的是最早
那行，会得出"没迁移"的错误结论——必须取 `max(version)`。

### 12.7 第三轮打包（2026-09-23，仅前端改动）

改动内容（纯前端，后端一行未动）：

1. `desktop/src/components/CandidateTable.tsx`：把行内「建提醒」按钮移进行尾「更多操作」
   菜单，紧跟在「下载」之后，与「解析表」同处一处。
2. `desktop/src/reminders/CandidateReminderDialog.tsx`：输入框加 `className="full"`；「保存提醒」
   按钮从 `.form-grid` 挪到 `.actions`——原先放在网格第二列会被拉伸成半屏宽。
3. `desktop/src/pages/TalentPoolPage.test.tsx`：新增回归用例锁定「建提醒」在「更多操作」里。

```
前端门禁  npx tsc -b → 退出码 0；npx vitest run → 179 passed (16 files)
安装包    desktop\src-tauri\target\release\bundle\nsis\recruit_0.1.0_x64-setup.exe（132,826,712 字节）
          SHA256 2B0956086F9873F5A4D0F674253F7A8DFCDC103A764FA91E2B3B0D650871EDD9
sidecar   与上一轮**逐字节相同**（A60443DF…FC10）——后端未改动，重建结果确定
安装      静默覆盖安装 setup.exe /S → 退出码 0
```

**本轮更正的一个重要事实（前几轮一直判断错了）**：应用**并不**使用 `E:\recruit\data`。
数据根由 `%APPDATA%\KeRuiRecruit\data_root.txt` 决定（见
`desktop/src-tauri/src/lib.rs` 的 `resolve_data_root`），当前其内容是
`C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data`。实测：

| 数据根 | 候选人 | 版本 | 新表 |
| --- | --- | --- | --- |
| `E:\recruit\data`（**弃用**） | 45 | v17 | 无 |
| `.dev-data`（**在用**） | 1511 | v22 | `candidate_reminder` / `daily_todo_check` 齐备 |

也就是说 12.6 里"生产库"指的其实是这份弃用库；真正的在用库 `.dev-data` 已于
2026-09-23 20:38 完成 v20→v22 迁移（自带 `recruit.pre-v20-to-v22-20260923-203831.sqlite3`
快照），且滚动已生效：1714 份已解析简历中 **1627 份建立了 `age_baseline`**。
本轮已额外把在用库备份到 `E:\devdata-backup-20260923-205434\`。**后续打包请备份
`.dev-data`，不要只看 `E:\recruit\data`。**

本轮验收：

| 项 | 结果 |
| --- | --- |
| 弃用库 `E:\recruit\data\db\recruit.sqlite3` | SHA256 与安装前一致（A004F578…D082），未被动过 |
| 已安装 sidecar 与构建产物 | 字节一致（A60443DF…FC10） |
| 已安装 `recruit.exe` | 与 `target\release\recruit.exe` 不同属正常：打包时 Tauri 会给 exe 打上 NSIS bundle 标记 |
| 应用启动 | `recruit.exe` + `kerui-recruit-sidecar` 均正常拉起，后端监听 127.0.0.1:55958（未带 token 访问返回 401，属预期） |
| 新前端已进包 | 构建产物 `index-w-A2p_jz.js` 中「建提醒 / 保存提醒 / 解析表 / 更多操作」均在 |

### 12.8 「建提醒」不能用：漏了 Content-Type（2026-09-23，第四轮）

**现象**：点「建提醒」→ 弹窗正常打开、能填内容，点「保存提醒」后弹窗不关，正文多出一行
`[object Object]`。

**定位过程**（不是靠读代码猜的）：起了一个隔离环境复现——把在用库复制到临时目录
（`config/encryption.key` 必须一起带，否则 `E_INTERNAL`）、用同一支 sidecar 代码跑
`python -m kerui_recruit.sidecar --port 43127`，再 `VITE_API_BASE_URL` / `VITE_SESSION_TOKEN`
指向它跑 `vite`，用浏览器真实点一遍复现。关键证据：弹窗里渲染出 `[object Object]` —— 说明
`ApiRequestError` 拿到的是**对象**而不是字符串。

**根因**：`desktop/src/api/client.ts` 里新加的两个写接口没有声明 `Content-Type`：

```ts
checkDailyTodo(input)          // POST /api/daily-followup/check
createCandidateReminder(input) // POST /api/candidate-reminders
```

它们只传了 `method` + `body`。fetch 在**没给 Content-Type 时会自动标成
`text/plain;charset=UTF-8`**，FastAPI 因此解析不出 body，回 422，且 422 的 body 是
`{"detail":[ ... ]}`——`detail` 是**数组**，`client.ts` 的 `request()` 写的是
`throw new ApiRequestError(apiError.message ?? apiError.detail ?? "...")`，把数组当消息塞进
Error，于是前端只看到 `[object Object]`。

实测对照（同一后端、同一 payload）：

| 请求 | 结果 |
| --- | --- |
| `Content-Type: application/json` | 200，返回提醒对象 |
| `Content-Type: text/plain;charset=UTF-8` | **422** `detail: [{"type":"model_attributes_type",...}]` |

全量核对方式：`(?s)method: "(POST\|PUT\|PATCH)",\s*body: JSON\.stringify` 只命中
1039/1046 两处（即上面两个方法）；其余 65 处写接口都带了 Content-Type。

**修复**：给这两个方法补 `headers: { "Content-Type": "application/json" }`。
`completeCandidateReminder` 没有 body，不受影响。**注意**：同类漏配会让「今日待办」勾选
也一起失效——所以两个都修了，不是只修报障的那一个。

**回归**：`tests/api-client.test.ts` 新增
`posts daily todo checks and candidate reminders as JSON`，断言两个请求的
`Content-Type` 为 `application/json` 且 body 正确。前端门禁 `tsc -b` 通过、
`vitest run` **180 passed**。

**浏览器端到端复验**（同一隔离环境）：保存后弹窗正常关闭；切到「数据看板」，
「我的提醒」列出现「马增鑫 · 下周一电话回访」；「追反馈」勾选后保持勾选、无报错，
`GET /api/daily-followup/today` 里该项 `done=true`。

**第四轮打包**：

```
安装包  recruit_0.1.0_x64-setup.exe（132,806,220 字节）
        SHA256 275A0CE7A99B353DA472097B944BDAA440E872A2C7C4CDB0D6A689F7CE313C67
安装    setup.exe /S → 退出码 0
备份    E:\devdata-backup-20260923-211319\（在用库）
```

| 项 | 结果 |
| --- | --- |
| 在用库 `.dev-data\db\recruit.sqlite3` | SHA256 与安装前一致（13AE180E…2831） |
| 已安装 sidecar 与构建产物 | 逐字节一致（A60443DF…FC10，后端未改） |
| 修复已进包 | 产物 `index-DaV9hdMt.js` 中 `application/json` 出现 67 次（修复前 65 次） |
