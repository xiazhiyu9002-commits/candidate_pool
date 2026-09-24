# AI 供应商可靠性、BD 助手与画像展示问题 — 执行方向

> **For agentic workers:** 本文是**分析结论 + 执行方向 + 实施记录**。按任务组逐项执行，
> 每组先复现失败、再做最小改动、最后记录证据，用 `- [ ]` 跟踪。
>
> **状态（2026-09-22 收口）：任务组 1–10 的全部条目均已勾选完成**，包括证据驱动判定为
> 「不必要 / 不处理」的 1.4、2.2、3.3、7.3、9.7。最后一批完成的是：
> - 2.2 视觉载荷真机判定（3/3 通过、零 `E_API_INPUT`，不做本地降质重试）；
> - 7.4 画像生成的阶段性反馈（SSE：`loading` / `draft` / `repair`）；
> - 7.6 首次生成 / 重新生成的耗时分布（见对应条目）；
> - 任务组 8.5 硬筛退化的 A/B（退化臂空结果 45.83% → 0%，救回 11 例、反向退化 0 例）。

**Goal:** 修复用户实测发现的 19 项问题，其中三条主线是：(A) 让非 DeepSeek 的 AI 供应商真正可用且可诊断；(B) 修好 BD 助手「重排后静默无数据」；(C) 修好画像文本被按标点切碎导致的展示问题。改完后用 `测试数据/` 目录做一次覆盖全部 AI 功能的端到端验收。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic / SQLAlchemy+SQLite / LanceDB / pytest；React 19 / TypeScript / Vite+Vitest / Tauri 2。

---

## 0. 已确认口径（用户已答复，直接按此执行）

| 编号 | 口径 |
| --- | --- |
| 第 7 点 | **只跳过订阅套餐**（Kimi 会员/Coding Plan、智谱订阅套餐、阿里订阅套餐）。智谱普通 API、Kimi 开放平台、火山引擎等按量付费平台**都要真实回归**。 |
| 第 9 点 | 不是加开关。需求是：**允许使用者把思考模型配置到「快速模型」槽位，并且能正常工作**。 |
| 第 5 点 | 沟通记录 = **单条自由文本**（覆盖式编辑），不是流水。 |
| 第 4 点 | 输入「数据工程师」→ **软排优先**，不做硬筛。 |

### 0.1 待确认的默认口径（如无异议按此执行）

1. **达标定义**（第 16 点的验收标准）：见 §6.1。
2. **沟通记录不参与检索**：不写进索引文档、不参与向量、不进画像输入哈希。
3. **沟通记录随可迁移包导出**（属用户数据）。
4. **存量画像采用 A+B+C 三段式**（已确认）：
   - **A 写入路径**：新解析与重新生成走新口径；
   - **B 展示路径**：前端对碎片做**只读合并**，不落库、不写索引；
   - **C 存量回填**：确定性脚本只做**相邻碎片拼接**（不调模型、不改字、不删字、不改顺序），跳过 `ai_profile_source == "manual"`，跑前备份 `db` 与 `search`，跑完对这批数据**重建一次向量索引**，可整体回滚。
   - 之所以 C 要重建索引：**每个分点都被独立向量化成一个子 chunk**（`search/documents.py:424-431`，`kind="profile_point"`），分点集合变化即向量库失效。这也是碎片分点在**拉低检索质量**的原因，不只是显示问题。
5. **文本输入治理**：给简历/JD 送模型前加字符上限，超限按「保头 + 保尾 + 中段省略」截断并在提示词里声明已截断。
6. **密钥不入库不入文档**：执行时由用户在应用内配置，或临时以环境变量提供；本文不记录任何 key。

---

## 1. 现场证据（本次分析已经坐实的部分）

### 1.1 你测的其实是 `.dev-data`，不是安装目录

已安装版的数据根记录在 `%APPDATA%\KeRuiRecruit\data_root.txt`，内容是 `C:\...\candidate_pool\.dev-data`（今天 11:49 仍在写）。`E:\recruit\data` 是 9/15 的旧库。本文全部现场数字来自对 `.dev-data` 的**只读**查询。

规模：1507 候选人 / 1711 简历 / 47 岗位 / 29 个 BD 会话 / 5424 个任务 / 21672 条匹配结果。

### 1.2 问题 3（BD 助手）：不是模型问题，是失败被静默吞掉

`bd_search_session` 最近 15 条：

| 时间 | 线索数 | 状态 |
| --- | --- | --- |
| 9/16 02:50（`大模型`） | **18**，`source=agent` | completed |
| 9/21 16:51 起 ~ 9/22 01:30，**连续 14 次** | **0** | completed |

14 次全为「静默完成」，前端于是渲染成「暂无线索」而不是报错。三个构造性断点：

- `bd_agent/agent.py:203-205` — 综合阶段拿不到结果（含 60s 超时）直接 `break`，跳过 `synthesized`/`leads`。
- `bd_agent/synthesis.py:98-101` — `except Exception: return SynthesisResult()`，**把模型异常吞成空结果**，随后仍发「综合出 0 条线索」→ 判完成。
- `runtime.py:435-437` — 综合阶段**强制 `REASONING_TEXT` + `ReasoningMode.REQUIRED`**；角色不满足时 `router.py:82-91` 抛 `E_AI_NO_PROVIDER`，被上一条吞掉。

→ 这解释了「快速模型和思考模型都一样」：**失败点在模型选择之上**。

测试缺口：`backend/tests/bd_agent/test_agent.py:167-172` 只断言到 `searching`/`done`，**未覆盖 `ranking` 之后**；前端没有 BD 助手的测试。

### 1.3 问题 1（画像换行）：后端按「逗号」切点 + 模型粒度不稳

真正的切分函数 `providers/profile_pair.py:200-207`：

```python
parts = re.split(r"[。！？；;，,\n]+", narrative or "")
```

正则含 **中文逗号 `，`**。前端每个分点渲染成独立一行（`ui/HoverText.tsx:116-117` 的 `.hover-text__line{display:block}` + `styles.css:615-616` 的分隔线），所以「分点被切碎」在界面上就等于「每遇到一个标点就换行」。

同一批数据里两种粒度混存（`.dev-data` 实测）：

```
points=6  3年经验Java高级后端工程师，现任京东后端开发工程师。      ← 整句
points=7  3年后端开发经验(8) / 现任京东科技后端开发工程师（P5）(17) / 长期处于互联网行业(9)  ← 碎片
points=8  此前任职众安保险(8) / 长期深耕金融科技(8) / 支撑多个应用与知识库(10)             ← 碎片
```

JD 侧同构：`desktop/src/pages/JdManagementPage.tsx:65-75`（两份重复实现），后端 `api/jd.py:459-471` 与 `api/jd.py:548-559`。另外发现 **1 个 JD 的候选人画像完全为空**（`points=0 narrative_len=0`）。

### 1.4 问题 10 / 8 / 13（检测与「显示可用但不解析」）

- 探测**严格串行 4 项、零重试、每项读超时 300s**（`providers/ai/probes.py:195-282` + `providers/ai/openai_chat.py:22-23`）。最坏 ≈ `10 + 4×300 = 1210s`。这就是「检测时间长」。
- 你看到的 `auth / text / json / reasoning / vision` 不是 5 个独立检测：**`auth` 是「第一个执行的能力探测」的结果复用**（`probes.py:258-280`）。所以「auth 正常、其余异常」是必然组合，不是一个额外故障。
- 「显示可用但不解析」的成因链：
  - JSON 探测只要求模型回 `{"ok": true}`（`probes.py:346-355`）；
  - **目录外模型**（如 `glm-5.3-flash`）走 `_generic_profile`，`supports_json_object=False`（`probes.py:324-332`），探测时**根本不发 `response_format`**，通过 ≠ 能产出简历大 schema；
  - 配置未变时**直接复用旧 `probed_roles`、完全不重探**（`api/ai_settings.py:221-223`）；
  - 界面状态只读 `probed_roles` + 熔断（`desktop/src/ai/AiServicesPanel.tsx:66-85`），与真实解析结果无关。

### 1.5 意外发现：失败高度集中在「视觉路径」

`task` 表 103 条 `PARSE_RESUME` DEAD_LETTER，规律很干净：

```
E_API_INPUT                    ← 100% 伴随 payload 里 "use_vision": true
E_AI_ALL_PROVIDERS_FAILED
```

`E_API_INPUT` 的判定词表是 `too long / context length / image size / invalid image / exceed`（`providers/errors.py:60-63`）。这说明 **问题 11/12/13 里「解析不合格 / 主备均不可用」有相当部分发生在走视觉重解析时被上游按「图片超限」拒绝**，不是模型能力问题。

对应根因：`providers/vision_parse.py:68-71` 把所有页图片**一次性塞进同一条消息**；且简历/JD 文本**全程无截断无分块**（`generation_tasks.py:184-186`、`jd/pipeline.py:39`）。

另一条「不合格」文案 `E_PARSE_INCOMPLETE` 来自 `resumes/validity.py:22-40`：要求 **≥3 个内容信号**且「技能 / 有意义 summary / 有内容经历」**至少 2 项**。

### 1.6 其余各项速查

| # | 结论 |
| --- | --- |
| 2 | 三个开关的解释文本在 `desktop/src/pages/TalentPoolPage.tsx:372-405`，常显 `.muted` 小字，**无 title / 无 tooltip**。已有可复用的 `HoverText`（`role="tooltip"`）；**没有** Popover/Tooltip 组件 |
| 4 | 简历解析 = 抽文本 → **1 次模型调用**（画像就是这次 JSON 的一个字段）→ 完整性门槛 → 身份去重 → 归一化。`direction/classifier.py` 是**纯规则词典、不调模型、只用于存量回填**；解析时方向**完全由 LLM 给**。「数据工程师」在**默认（AI 解析关闭）下不产生任何方向条件**；开启后若模型给出方向会被采纳 |
| 5 | **不存在**候选人级备注表。最近似的是流程级 `candidate_job_case.note` 与事件级 `case_event.note`，都挂在流程上 |
| 6 | 岗位画像走**强制思考模型**（`runtime.py:403-409`），`produce_pair_with_vet(max_repairs=2)`（`profile_pair.py:210-245`）→ **最多 3 次调用**，每次读超时 300s，**全链路无 deadline、无并发**；「重新生成」还会额外拼入 `previous` + `INCREMENTAL_RULES`（`profile_spec.py:46-53,162-169`），prompt 更长 |
| 9 | 目前**完全不存在**该能力。卡点之一是 `catalog_models.can_serve_role`（`catalog_models.py:48-62`）：目录内模型必须声明该角色，`zhipu` 的 `glm-5.3` 只声明 `reasoning_text`，填进 `fast_text` 会被 `validation.py:35-40` 拒绝 |
| 16 | `测试数据/` = 5 份简历（2 docx + 3 pdf，最大 584KB）+ `JD导入测试.xlsx` + `JD测试.docx` + `mapping测试文本.docx` + `新建 文本文档.txt`。⚠️ 最后那个 txt 里是**得物算法团队 JD + 组织汇报信息**，不是单纯 JD |

---

## 2. 执行方向：任务组

优先级：**P0 = 阻塞验收**，**P1 = 用户明确点名**，**P2 = 质量优化**。

### 任务组 1（P0）#3 BD 助手：让失败可见并修断链 ✅ 已完成

- [x] **1.1 停止静默吞异常**。`bd_agent/synthesis.py` 不再 `except Exception: return SynthesisResult()`，异常直接抛出，由 `BdAgent` 统一处理。
- [x] **1.2 暴露降级原因**。新增 `BdAgent._synthesis_step()`（返回 `(结果, 失败原因)`）与 `AgentResult.degraded_reason`；失败/超时发 `synthesis_failed` 阶段事件；`AgentQueryResponse` 与 SSE `result` 都带上 `degraded_reason`。
- [x] **1.3 前端区分两种空**。`BdAssistantPage` 空态分两支：有 `degraded_reason` → 「线索综合失败」+ 原因 + 排查提示；否则 → 「暂无线索」。
- [x] **1.5 补测试**。后端：全阶段断言（含 `ranking`/`synthesizing`/`synthesized`/`leads`）、ProviderError 暴露、超时留痕、已攒线索保留、API 与 SSE 的 `degraded_reason`、综合角色钉死、预算关系守卫。前端：新增 `BdAssistantPage.test.tsx`（3 条，覆盖两种空态与有线索时不显示空态）。
- [x] **1.6 真机复现**。火山引擎连接实测：**修复前 0 条线索 + `degraded_reason=线索综合超过 60 秒未返回`；修复后 5 条线索（conf 0.75~0.9、均带 2~3 条证据）、无降级、耗时 145 秒。**
- [x] **1.4 角色降级 → 判定为不需要**（证据驱动）。真机复现证明角色本身可用，`reasoning_text` 探测正常，失败纯粹是**思考模型太慢**。实际修复分两步：
  1. `_SYNTHESIS_TIMEOUT_SECONDS` 60 → **180**、`_DEFAULT_DEADLINE_SECONDS` 120 → **240**（总预算必须大于单阶段上限，否则 `min(上限, 剩余)` 会先被总预算掐掉）。**只改这一项仍然不够**：实测 200 秒后依旧超时。
  2. **综合改用快速模型**（`runtime.py`）。原设计按「需要跨片段取舍」把综合放在推理档 `ReasoningMode.REQUIRED`，但那条取舍其实已由上一步 `bge-reranker-v2-m3` 完成，综合只剩「抽取 + 逐字引用」；且 #9 完成后用户可自行把思考模型填进快速槽位来换取更强推理。改完即通过。
- 附带：综合前新增 `synthesizing` 进度事件，避免 1~2 分钟的安静等待看起来像卡死。

**测得的环境事实**（对后续任务组有用）：用户当前连接是 `custom_openai`（火山引擎 Ark，`parameter_style=standard`），三个模型都不在内置目录里，因此 `apply_json_format` 会走「未声明 JSON 能力」分支——**`response_format` 从来不发**，结构化输出完全靠提示词 + 容错解析。这是任务组 3.7 要解决的核心问题。

### 任务组 2（P0）视觉路径失败 → 问题 11/12/13 的主要成因 ✅ 已完成（含 2.2 真机判定）

- [x] **2.1 图片分批 + 体积治理**。
  - `providers/ocr.py`：`rasterize_pdf` 改为渲染 **JPEG** 并新增 `max_edge` 上限（`_MAX_IMAGE_EDGE = 1568`，视觉模型的通用最优边长），新增导出常量 `RASTER_IMAGE_MIME` 供调用方拼 data URL。
  - `providers/vision_parse.py`：结构化视觉解析按 `_MAX_PAGES_PER_CALL = 4` **分批发送**，多批结果在本地合并（列表字段按批序拼接去重，标量字段取首个非空）；4 页以内（绝大多数简历）仍然只发一次请求。
  - 删掉 `vision_parse.py` 里从未被引用的 `_PAGE_TIMEOUT_SECONDS`，以及两个未使用的 import。
- [x] **2.3 文本输入治理**。`providers/generation_tasks.py` 新增 `truncate_model_input()`，`render_resume_parse_prompt` / `render_jd_parse_prompt` 送模型前按 `_MAX_MODEL_INPUT_CHARS = 20000` 截断，**保头 70% + 保尾 30%**，并在正文里显式写出省略了多少字符。JD 拆分（`split_jds`）**不截断**——截断会破坏拆分结果，而该路径本来就有确定性兜底（`split_jd_text`），失败也能工作。
- [x] **2.2 超限本地重试 → 判定为不必要（证据驱动）**。
  口径是「先按有界载荷修（2.1），再实测 3 份 PDF 是否还出现 `E_API_INPUT`；只有仍然出现才实现
  本地降质重试」。实测（`.tmp-api/verify_vision_payload_2026_09_22.py`，走生产同一条视觉路径
  `VisionStructuredParser` → `rasterize_pdf` → 分批 data URL，真调视觉模型，供应商 = 阿里
  `qwen3.8-flash`）：

  | 文件 | 页 | 单页 KB | 最大批 KB | 最大批 base64 | 结果 |
  | --- | --- | --- | --- | --- | --- |
  | 项目管理代表-胡凯-MGA.pdf | 2 | 235 / 101 | 336 | 0.44 MB | ✅ 17.2s，字段非空 29/42 |
  | 马增鑫-1990-信息安全.pdf | 3 | 153 / 191 / 70 | 414 | 0.54 MB | ✅ 21.1s，字段非空 28/42 |
  | 马瑞强-大华-项目经理.pdf | 4 | 294 / 295 / 276 / 184 | 1049 | 1.37 MB | ✅ 25.0s，字段非空 27/42 |

  **3/3 成功、`E_API_INPUT` 0 次**，姓名全部识别正确。对照修复前同一份 4 页文件
  （PNG@200 合计 1953 KB → base64 **2.54 MB**，且单页边长 1654×2339 越过常见的 2048 上限）：
  现在是 1049 KB → 1.37 MB、边长 1109×1568。所以按口径**不实现**本地降质重试，
  不凭猜测加代码。

**实测数据（修复前）**——`测试数据/resumes_batch` 三份 PDF 在 DPI 200 下栅格化：

| 文件 | 页数 | PNG@200 单页 | PNG@200 合计 | base64 请求体 |
| --- | --- | --- | --- | --- |
| 项目管理代表-胡凯-MGA.pdf | 2 | 190~496 KB | 687 KB | 0.89 MB |
| 马增鑫-1990-信息安全.pdf | 3 | 127~400 KB | 834 KB | 1.09 MB |
| 马瑞强-大华-项目经理.pdf | 4 | 326~608 KB | 1953 KB | **2.54 MB** |

改后同一份 4 页文件：**JPEG@1568 → 1049 KB / base64 1.37 MB**，且页面边长 1109×1568 落在通用上限内（原来 1654×2339 超过常见的 2048 图片边长限制）。

**新增测试**：`tests/providers/test_vision_parse.py`（单批/分批合并/JPEG mime）、`tests/providers/test_generation_tasks.py`（截断保头保尾、省略计数、正常正文不误伤）、`tests/providers/test_ocr.py` 更新为 JPEG 并新增「最长边受上限约束」断言。

### 任务组 3（P0）AI 检测与角色：问题 8/9/10/17 ✅ 已完成（3.3 判定为不必要，面板展示见任务组 5）

- [x] **3.1 检测并发化**。`probes.py` 的 text / reasoning / vision 三项改为 `asyncio.create_task` 并发发起（json 依赖 text 成功，仍串在 text 之后）。原先严格串行，四项最坏 20 分钟。
- [x] **3.2 探测专用超时**。新增 `_PROBE_TIMEOUT_SECONDS = 45.0`，三项共享一个 `deadline_monotonic` 传给适配器；不再按业务调用的 300 秒算。整轮最坏 ≈ 模型发现 10 秒 + 45 秒。
- [x] **3.4 #9 放开快速槽位**。`can_serve_role()` 新增分支：`fast_text` 槽位放行「强制思考（required）」模型。原先智谱 `glm-5.3` 这类只声明 `reasoning_text` 的模型填进快速槽会被保存前 422 拒掉。参数映射无需改动——强制思考模型没有「关思考」开关，`apply_reasoning` 对它不发送任何思考字段，按模型自身默认行为运行。
  - **反转了一条旧断言**：`test_reasoning_model_in_fast_text_is_422` → `test_thinking_model_in_fast_text_is_accepted`，并新增 `test_unrelated_model_still_rejected_for_vision_slot` 守住「只放开快速槽位、不顺手放开视觉槽位」。
- [x] **3.5 能力矩阵落盘**（后端部分）。`AiConnection` / `AiConnectionPublic` / `_StoredConnection` 新增 `probed_capabilities: dict[str, bool]`；`api/ai_settings.py` 新增 `_capability_matrix(report)` 并在保存时写入，`ConnectionResponse` 回给前端。**服务面板的可视化展示留给任务组 5**（同属前端改动）。
- [x] **3.6 探测缓存加版本**。新增 `PROBE_SCHEMA_VERSION = 2`；`_connection_changed()` 增加「已存 `probe_version` 落后于当前版本 → 必须重探」。没有这一条，JSON 探测从 `{"ok": true}` 收紧为业务同形对象后，老连接会一直显示按旧口径算出的「可用」。
- [x] **3.7 JSON 能力探测加强**。`_ProbeJson` 从 `{"ok": true}` 升级为业务同形小对象（`name` 必填 + `skills` 数组 + 可空 `total_years`），提示词同步给出格式示例。这直接治「显示可以用但导入简历一直不解析」：能回业务 JSON 才给 `fast_text` 角色，否则保存阶段就以 `E_AI_UNUSABLE_CONFIG` 明确拒绝，而不是让用户反复试。
- [x] **3.3 检测分级 → 判定为不必要**（证据驱动，同 1.4 / 2.2 的处理方式）。3.1+3.2 已把整轮检测的最坏值从**串行 4 项 × 300 秒**压到**并发 3 项 + 45 秒封顶**，含模型发现最坏 ≈ 55 秒，已经满足 §6.1 的 ≤60 秒目标。再拆「快速检测 / 完整检测」会引入一个新状态：`reasoning_text`/`vision` 角色**尚未探测**。而「保存时角色未知」正是本轮要消灭的「显示可用但实际不解析」——为了省下已经不多的秒数而重新引入这类中间态，不划算。若后续要在低配机器上再压时间，优先考虑「三个角色都已显式指定时跳过 `/models` 发现」（省约 10 秒），而不是拆分级。

**注意**：3.7 收紧后，探测矩阵里 `json` 变红是**真实信息**，不是新故障。对目录外模型（如火山引擎的自定义模型），`response_format` 本来就不发送，JSON 探测测的就是「提示词驱动的 JSON 合规性」——这正是真实解析会走的路径。

### 任务组 5（P1）#2 解释文本改悬停 + 3.5 面板展示 ✅ 已完成

- [x] **5.1 新增 `InfoTip`**（`desktop/src/components/ui/InfoTip.tsx`）。没有复用 `HoverText`：后者是「长文本单元格」的预览模式，**只在内容被截断时才弹层**、且按行渲染正文；这里要的是「无论多短都弹」的静态说明。
- [x] **5.2 三个开关改造**。`TalentPoolPage` 的「检索经历正文 / AI 语义改写 / AI 智能解析」不再常显 `.muted` 说明文字，改为 ⓘ 图标 + 悬停弹出。
  - 结构上把 ⓘ 放在 `<label>` **外面**（包一层 `.rewrite-toggle-group`）：`<button>` 属于 labelable 元素，嵌在 `<label>` 里是非法 HTML。
- [x] **5.3 可达性**。键盘 `Tab` 聚焦 ⓘ 也会弹出说明（`onFocus`/`onBlur`）；`focus-visible` 有可见焦点环。
- [x] **5.4 测试**。`TalentPoolPage.test.tsx`：三条说明默认都不渲染 → 悬停 ⓘ 后出现 → 移出后消失；键盘可聚焦并弹出说明。原断言「常显说明文字」的那条已改为断言「默认不占位、ⓘ 在」。
- [x] **3.5 前端展示**（补完后端那半）。`AiServicesPanel` 在每条连接下渲染能力矩阵（鉴权 / 文本 / JSON / 思考 / 视觉 + ✓/✗）。
  - 关键细节：**只展示本次真的探测过的项**。没配视觉模型时根本不会探测视觉，渲染成红色 ✗ 会被误读为「视觉不能用」——那是「没测」不是「失败」。
  - 老配置（按旧口径保存、没有矩阵）不渲染任何 chip，不制造噪声。

**实施中踩到的两个坑（都已修，记录以备回归）**：

1. **ⓘ 的可访问名称不能取说明正文，也不能与切换项名称互为子串**。第一版把 `aria-label` 设成「AI 语义改写 说明」，导致 Playwright `getByLabel("AI 语义改写")` 命中两个元素（strict mode violation）；第二版改成说明正文，又因为「工作职责 + 项目描述（**仅影响关键词**通道）」含「关键词」二字，让 `getByRole("button", { name: "关键词" })` 命中信息图标。最终用**固定名称 `查看说明` + `aria-describedby` 关联说明浮层**，并给浮层加 `id`。
2. **`tests/search.spec.ts` 的姓名断言依赖测试执行顺序**。3.7 修好 JSON 探测后，`tests/ai-settings.spec.ts` 的 mock 连接从「保存被拒」变成「保存成功」，于是后续导入的简历走 AI mock，姓名从简历原文变成 mock 载荷里的「测试候选人」——两条用例从「单跑通过」变成「整轮跑失败」。已改为按**表格结构**定位结果行（`columnheader "AI 画像"` 所在表的第 1 行），它本来测的是「导入 → 入索引 → 搜得到」，姓名属于环境相关量，不该断言。

### 任务组 6（P1）#5 候选人沟通记录 ✅ 已完成

- [x] **6.1 数据模型**。`candidate` 表新增 `communication_note TEXT`；schema 迁移 **19 → 20**（`db/upgrades.py::_upgrade_v19_to_v20`，沿用 `_add_column` 幂等写法）。
- [x] **6.2 API**。新增 `PUT /api/resumes/candidate/{id}/communication-note`（body `{note}`，空串即清空）。
  - **刻意不复用** `PUT /candidate/{id}/field`：后者编辑 `parsed_data`，会触发 `ai_profile_stale` 判定与索引重建入队；沟通记录是候选人级备注，放进 `parsed_data` 等于把主观判断喂给画像与向量。
  - 校验：候选人不存在 → 404 `E_CANDIDATE_NOT_FOUND`；`note` 上限 2000 字符。
- [x] **6.3 前端分栏**。`CandidateTable` 的 AI 画像单元格改为上下两栏：上=AI 画像（原有 `HoverText` + 来源徽标），下=沟通记录（`CommunicationNoteCell`，双击进入多行编辑、失焦保存）。`CandidateListItem` 与 `CandidateSearchItem` 都带上 `communication_note`，人才库列表与搜索结果一致。
  - 搜索结果侧的字段是**零额外查询**：构造 `CandidateSearchItem` 时 `candidate` ORM 对象已在作用域内，直接取 `candidate.communication_note`。
  - 沟通记录行加了虚线分隔与弱化配色，避免被误读成画像的一部分。
- [x] **6.4 边界**。进画像输入、索引文档、向量均**不包含**该字段；`db/models.py` 的字段注释写明理由。
- [x] **6.5 可迁移包**。可迁移包是整库导出，新增列自动包含，无需额外改动。
- [x] **6.6 验证**。新增 `tests/api/test_candidate_communication_note.py`（4 条）：列表与搜索都能读回；**`parsed_data` 与画像状态一字未变**；**沟通记录搜不到**（含「对照搜索能命中」的前置断言，避免空断言假通过）；空串清空 + 404。前端补 1 条：双击编辑后只调用沟通记录接口、**不调用 `onUpdateField`**。
- [x] **6.7 精确筛选加「沟通文本」搜索框 ✅（用户 2026-09-22 追加）**。按**子串（LIKE）**匹配沟通记录。
  - `CandidateFilters` 新增 `communication_note: str | None`；`CandidateFiltersRequest` 同步（前端据此传参）。
  - **只能在 SQLite 层过滤**：沟通记录刻意不进索引（6.4），所以照 `phone`/`gender` 的既有做法，
    在 `_resolve_structural_candidates` 里先解析出候选人 id 集合、与其它结构条件取**交集**，
    再用 `candidate_ids` 收窄索引检索。筛不到人时走既有早退分支返回 `no_match`，不静默忽略条件。
  - 匹配用 `ilike` 并**转义** `\` `%` `_`：不转义时用户输入 `%` 会变成「匹配所有人」。
  - `_hydrate_hits` 增加实时复核（与 phone/gender 同理由：索引投影不是事实来源）。
  - 前端：`SearchFilterDraft.communicationNote` + 面板输入框；**`CONDITION_DRAFT_KEYS` 必须同步加映射**，
    否则「生效硬条件」标签上的「改」「×」按钮会因映射缺失静默失效。
  - 验证：后端 3 条（子串命中、大小写不敏感 + `%` 转义守卫、空白等价于无条件）+ 前端 2 条
    （输入写回草稿、请求体带 `communication_note`）。

### 任务组 4（P1）#1 画像分点口径 ✅ A+B 已完成，C 的 dry-run 已出待确认

**方案修订（实施中改的）**：原计划的 4.2「按长度把过短分点并入相邻分点」**被放弃**。
现有测试 `test_build_profile_pair_splits_lines_and_derives_compact` 就守着它的反例：
`"7年后端研发\n主导交易系统\n精通 Java 微服务"` 三个分点都短于 12 字，但它们是**完整且合法**的
分点（由换行显式分隔），按长度合并会把它们挤成一行。长度无法区分「合法短句」与「逗号切出的碎片」，
所以改成**可判定的修复判据**（见下方 `repair_profile_points`）。

- [x] **4.1 后端切分收敛**。`_SENTENCE_BREAK = [。！？；;\n]+`，`split_profile_clauses` 改为
  `split_by_sentence`。（`、` 与 `，` 都不再切分。）
- [x] **4.2 改为「可判定的存量修复」**。`repair_profile_points(narrative, points)`：
  只有在**同时**满足「存在短于 12 字的分点」且「分点用『。』拼接后不等于整体段落」时才重算。
  第二条防止无意义改写（短但合法的整句分点拼起来正好等于原段），
  也防止把模型产出的「核心 3~5 条」子集扩成全部句子（那是改语义，不是修显示）。
- [x] **4.4 提示词侧约束**。`DUAL_FORM_CONTRACT` 显式写明：分点粒度固定为句子，
  **不得把一个句子按逗号、顿号或分号拆成多条**，并举了「此前任职众安保险」这类反例。
- [x] **4.6 展示层修复（B）→ 实现为后端读取时修复**。`repaired_profile_view(parsed_data)` 在
  `GET /api/resumes/candidates` 与搜索结果两处应用，**只改 `ai_profile_points`、不写库**。
  放在后端而不是前端：切分口径只能有一份实现，否则前后端各写一套必然漂移。
- [x] **4.7 存量回填脚本（C）**。`scripts/repair_profile_points_2026_09_22.py`，
  `--dry-run` / `--apply` 二选一；跳过 `ai_profile_source == "manual"`；每改一条
  `enqueue_sync("candidate", id)`，由应用启动后的 outbox 完成索引投影；报告写 `.tmp-plan/`。
- [x] **4.7b 正式回填 ✅ 已执行**（2026-09-22，用户确认）。先备份
  `.dev-data/backups/backup_20260922T060000Z_回填画像分点前.sqlite3` + 同名 `.manifest.json`，
  再 `--apply`：落库 668 条、入队索引重建 668 条。
  **索引未做 37GB 全量拷贝**（C 盘当时只剩 42GB，拷完仅余约 5GB）：索引是 DB 的**派生产物**
  （chunks 由 `parsed_data` 推导），所以回滚口径是「还原本 DB 备份 + 重跑同步」，与拷贝等价且确定。
  这条偏差已写进备份目录的 manifest。
- [x] **4.8 回填后重建索引 + 检索前后对比 ✅ 已完成**。
  重建：`scripts/rebuild_index_2026_09_21.py`（634 条待投影 → 26 轮 → 425s，然后 FTS 优化 7s），
  最终 `待投影=0 失败=0`，FTS 探针逐词与重建前**完全一致**（深圳 199 / 数仓 88 / 数据仓库 96 /
  前端 69 / java 500）。
  对比：`.tmp-plan/profile_point_ab_2026_09_22.py`（同一脚本、同一批抽样、同一组查询各跑一次）。

  | 指标 | 回填前 | 回填后 | 判读 |
  | --- | --- | --- | --- |
  | 抽样候选人索引行数 | 26.3 行/人（min 13） | 22.27 行/人（min 10） | 碎片合并生效 |
  | `profile_point` 行数 | 375 | **214（−43%）** | 粒度合并的直接证据 |
  | 抽样候选人 present | 40/40 | 40/40 | **没人掉到 0 行**（画像没被索引丢掉） |
  | 12 条查询空结果数 | 0 | 0 | 无「非空→空」退化 |
  | top-20 平均 Jaccard | — | 0.948 | 排序基本不动，3 条查询小幅变动（0.74~0.82） |

  结论：**粒度修好了，召回没有退化**。报告见 `.tmp-plan/profile-point-ab-2026-09-22-{before,after}.json`。
- [x] **4.3 前端口径统一 ✅**。`CandidateTable.profilePoints` 改为导出，`JdManagementPage`
  删掉重复的 `resumeProfilePoints` 改为 import——同一候选人在人才库与岗位管理两处不再可能显示不同。
- [x] **4.5 空画像兜底 ✅**。两处画像单元格在「既无分点也无整体段落」时显示 `未生成画像`
  而不是 `HoverText` 的默认 `—`：实测确有画像内容为空的记录，裸露的 `—` 会被读成「字段不存在」。
- [x] **4.9 验证**：回填脚本单测 + 切分口径单测 + 上方真机 A/B 对比。

**dry-run 结果（`.dev-data`，2026-09-22）**：

| 项 | 数值 |
| --- | --- |
| 扫描 READY 修订 | 1710 |
| **需要修复** | **668** |
| 跳过人工编辑 | 3 |
| 无分点的修订 | 0 |

典型 diff（修复前 → 修复后分点数）：

```
7 条 → 3 条：8年算法工程师 / 现任字节跳动高级算法工程师 / 深耕风控算法与AI安全 / …
               ↓
             8年算法工程师，现任字节跳动高级算法工程师，深耕风控算法与AI安全 / …
10 条 → 5 条：16年质量工程经验 / 现任美团金服质量架构师 / 此前任职思科、美团点评 / …
```

报告全文：`.tmp-plan/repair-profile-points-2026-09-22.json`。

### 任务组 7（P1）#6 / #11.2 画像生成提速 ✅ 7.1–7.6 全部完成

「重新生成画像」是**交互式**入口，所以核心是把最坏情况的调用次数与墙钟时间都收到有界。

- [x] **7.1 加总预算**。`produce_pair_with_vet` 新增 `budget_seconds`（默认
  `REPAIR_BUDGET_SECONDS = 90`），初稿 + 重写共用；超预算**只用来决定要不要再发起一次重写**，
  不能中断已在飞的那次调用（那由 provider 读超时兜住）。`budget_seconds=None` 表示不限预算，
  给批量回填这类非交互链路留出口；`0` 是「一次重写都不许」的有效取值（判空用 `is not None`）。
- [x] **7.2 降重写次数**。`max_repairs` 默认 2 → 1，总调用数上界从 3 收到 **2**；
  原先为「仅超字数」额外放行的那次不再存在（那正是第三次调用的来源）。
- [x] **7.5 服务端超时与文案**。`regen-profile`（JD 侧与候选人侧）都套上
  `asyncio.wait_for(..., REGEN_TIMEOUT_SECONDS = 150)`，超时返回
  `504 E_PROFILE_TIMEOUT` + 「画像生成超过 150 秒未返回，已放弃本次生成；原画像保留，请稍后重试」，
  界面不再无限转圈。`REGEN_TIMEOUT_SECONDS > REPAIR_BUDGET_SECONDS` 这条关系有守卫测试
  （`test_regen_timeout_exceeds_repair_budget`）：反过来的话预算还没用尽接口就先超时，闸门形同不存在。
- [x] **7.3 重新生成路径瘦身 → 评估后决定不改**。`previous` + `INCREMENTAL_RULES` 让 prompt 显著变长，
  但 JD 侧 `previous` 取的是 `candidate_profile`，而它与分点本就同源
  （`pair_from_points` 由分点拼出整段），改传分点省不下多少 token。
  7.6 的实测也印证了：真正的耗时来自模型调用次数与输出长度，不是这几百字的 prompt。
- [x] **7.4 进度可见 ✅ 已实现（阶段性反馈，不是转圈）**。
  `regen-profile`（岗位侧 `/api/jd/{id}/regen-profile` 与候选人侧
  `/api/resumes/candidate/{id}/regen-profile`）在 `Accept: text/event-stream` 时改为 SSE，
  先推阶段再推结果：

  | stage | 含义 |
  | --- | --- |
  | `loading` | 正在读取原文（最新 READY 版本） |
  | `draft` | 正在生成初稿（**第一次**模型调用） |
  | `repair` | 初稿未过校验，正在定向重写（**第二次、也是最后一次**调用） |

  关键设计：

  1. **`repair` 只在真的还有第二次调用时才发**。这正是使用者要的判断依据——最坏 150 秒，
     「还会不会再来一轮」决定预期等待差一倍。多报一次这条信号就作废（守卫：
     `test_produce_pair_with_vet_reports_stages_only_when_a_rewrite_happens`）。
  2. **复用同一个路径做内容协商**，而不是新开 `-stream` 路由：新路径会改变
     `scripts/api_inventory_2026_09_21.py` 导出的路由清单，而「未覆盖路由 = 0」是 10.2 的闸门；
     SSE 只是同一处理器的传输变体，不是新能力。不带 `Accept` 时响应逐字不变
     （守卫：`test_profile_stream_without_accept_header_stays_json`）。
  3. **错误在流里回传**（`error` 事件带 `status`/`code`/`message`）：响应头一旦发出就改不了
     状态码，不这样做客户端只会看到一条断流、错误信息全丢。异常→ApiError 的映射**两条路径共用
     同一个函数**，同一个失败在流式与非流式下给出相同的码与文案。
     顺带把 `ProviderError` → HTTP 状态的规则从 `main.py` 上收成 `ProviderError.http_status`
     （原先只写在全局处理器里，SSE 路径必须用同一套）。
  4. 客户端断开时**取消后台任务**、不再烧模型调用；且不在已关闭的异步生成器里 `await task`
     （那会抛「async generator ignored GeneratorExit」，把正常断开变成 500）。

  改动面：`providers/profile_pair.py`（`on_stage` 同步回调）、`backfill/service.py`、
  两个生成器（`resumes/profile.py` / `jd/profile.py`）、新增 `api/profile_stream.py`、
  `api/jd.py` / `api/resumes.py`（内容协商）、前端 `client.ts`（`streamProfileGeneration`）
  + 两处编辑器的阶段文案展示。测试：`test_jd_profile_stream_reports_stages_then_result`、
  `test_jd_profile_stream_returns_errors_as_events`（后端）、
  `test_profile_stream_reports_stages_then_result`、`test_profile_stream_without_accept_header_stays_json`（后端）、
  `shows profile generation stages while regenerating`（前端）。
- [x] **7.6 验证（真机，2026-09-22）✅ 完成，并纠正了一处更上游的问题**。
  脚本 `.tmp-api/measure_jd_profile_distribution_2026_09_22.py`（只读：两条臂都只产预览、不落库）
  在同一台机器、同一批 JD 上跑两条臂，都按**同一条交互式上限** `REGEN_TIMEOUT_SECONDS`（150s）截断：

  | 臂 | 调用 | prompt |
  | --- | --- | --- |
  | `first` | `jd_generator.generate_pair(data)` | 不带 `previous`（首次生成的形态） |
  | `regen` | `backfill_service.regenerate_jd_profile(jd_id)` | 带 `previous=candidate_profile`（更长） |

  **第一轮（岗位画像还在推理档）直接暴露问题**：

  ```
  01a0c8b0 first  ⏱ 撞上 150s 上限（阶段 ['draft']）| 测试工程师（夹具）
  ```

  阿里 `qwen3.8-max`（推理档 + `ReasoningMode.REQUIRED`）150 秒没返回、阶段停在 `draft`，
  也就是 `POST /api/jd/{id}/regen-profile` 在这家供应商上会**稳定 504 `E_PROFILE_TIMEOUT`**，
  §6.1 的「单次生成 ≤ 90 秒」达不到——7.1/7.2 只把调用次数从 3 收到 2、加了预算闸门，
  改不了单次调用本身的长度。**用户 2026-09-22 选定「改走快速档」**，据此改
  `runtime.py` 的 `jd_generator`（推理档 + REQUIRED → 快速档），并加守卫测试
  `test_jd_profile_uses_fast_model_to_stay_within_the_interactive_budget`。

  **第二轮（快速档）实测**：

  | 臂 | n | 耗时 | 模型调用 | 分点 | 命中 150s 上限 | 超 90s 达标线 |
  | --- | --- | --- | --- | --- | --- | --- |
  | `first` | 2 | 4.6 / 4.9s（中位 4.8s） | 2 次 | 3 条 | 0 | 0 |
  | `regen` | 2 | 4.0 / 4.9s（中位 4.5s） | **1 次** | 3 条 | 0 | 0 |

  对照改动前的结构（最坏 3 次调用、无墙钟预算、无接口上限）与第一轮的 150s 超时：
  **从「150 秒还出不来」变成 ~4.5 秒出结果**。`regen` 只需 1 次调用，是因为它带
  `previous`、初稿本来就接近契约，一次过校验。

  **遗留观察（2026-09-22 已定位并修掉，见下）**：快速档产出的 `narrative` 长度是
  **47~62 字**，低于 `DUAL_FORM_CONTRACT` 的 80~150 字区间，而定向重写（第二次调用）
  **修不动**（日志：`画像修正后未改善（(1, 0) → (1, 0)），保留更好的版本 … 长度 47 字，不足下限 80 字`）。

  **根因是两个真缺陷（不是「提示词口径」——80~150 字提示词里早就写了）**：

  1. `profile_spec.render_rewrite_instruction` 的「只做一件事」分支**只认超字数**：
     `max(current - high, low - current)` 在「当前 47 字、区间 80~150」时算出 33，
     指令成了「**删掉约 33 字**」——而这份文本总共只有 47 字、目标是补 33 字，
     方向完全反了。函数 docstring 里写的「只做删句/**补句**」从来没实现。
     修法：按方向拆两支，不足下限走「这次只做补充：当前 47 字，需补足约 33 字；
     只补写证据里已有、画像没写进去的条件，不得编造、不得改写已有句子」。
  2. `profile_spec.issue_severity` 的偏离量**只统计超出量**（`_VET_LENGTH_OVER` 只匹配
     「超过上限」），不足下限时恒为 0 → 47 字与 79 字得到同一个 `(1, 0)` →
     `produce_pair_with_vet` 里 `fixed >= best` 成立 → **一次真的补进了 32 字的重写会被
     当成「未改善」丢掉**。修法：新增 `_VET_LENGTH_UNDER`，偏离量两侧对称。

  守卫测试：`test_render_rewrite_instruction_switches_to_append_when_under_length`、
  `test_issue_severity_counts_shortfall_too`。

  **修复后复测（同一批 JD、同一家供应商，3 条 × 2 臂）**：

  | 臂 | n | 耗时 | 模型调用 | 正文 | 80~150 校验 |
  | --- | --- | --- | --- | --- | --- |
  | `first` | 3 | 5.1 / 5.4 / 7.1s | 2 次 | **104 / 104 / 103 字** | **3/3 通过** |
  | `regen` | 3 | 5.4 / 5.9 / 6.0s | 1~2 次 | **112 / 110 / 124 字** | **3/3 通过** |

  即 **47~62 字、校验全不过 → 103~124 字、6/6 通过**，耗时仍只有 5~7 秒。
  `repair` 阶段在 6 条里出现 5 次 —— 说明补句分支是承重的，不是装饰。

  **新观察到的小项（未改）**：`points` 有时只有 **2 条**，低于契约的「3~5 条」。
  `vet_profile` 目前**不检查分点数**（只查正文长度/形态/禁写词），所以这一项不受闸门约束；
  要不要补一条校验是另一个决定。

  **接口探针复核**（`--only jd,resumes`，改完之后）：`jd.regen_profile` 从原来的
  **504** 变成 **200**；该模块 38 条探针 **pass=38 / fail=0 / error=0 / 未覆盖=0**。

### 任务组 8（P1）#4 方向 / 业务方向 / 公司 / 职位等：先判断能否硬筛，不能则软排 ✅ 8.1–8.5 全部完成

**口径（用户 2026-09-22 修订，覆盖原先「一律软排」的写法）**：查询解析产出的这些字段，处理顺序是
**「先判断这一条能不能安全地硬筛 → 能就硬筛 → 不能就退化成软排加权」**。
判断「能不能」的判据是**失败开放**：硬筛不得把结果清空。这与代码库里既有的三条纪律同源——
`_apply_rerank_min_score`、`_apply_vector_absolute_fallback`、混合检索的概念闸门失败开放
（见 `2026-09-21-query-parse-hybrid-body-design.md`）。

适用范围（同一套判据，不是只改方向）：
- **职业方向 / 职业细分 / 业务方向**：维度枚举有限、值可穷举，适合作硬筛；但首次硬筛若把结果清空，
  该条退化为软排。
- **公司 / 职位**：自由文本，硬筛（子串/精确）很容易一条都不命中；只有当条件足够具体、
  且硬筛后仍有结果时才当硬筛用，否则退化为软排。

#### 实现（已落地）

判定层是 `search/service.py:HybridSearchService._relax_unmatchable_filters`，逐条**探测**：
拿掉这条条件后剩余硬条件组合还筛不筛得出人。能 → 它就是把结果筛空的那条 → 记退化；
不能 → 说明与此条无关，保留硬筛看下一条。顺序由 `_RELAXATION_ORDER` 决定，
**最不可靠的先退**：`company` → `title` → `business_directions` → `career_directions`
→ `career_specializations`（公司/职位是整串子串匹配，方向是有限枚举且有专门索引列）。

- [x] **8.1 逐条判定 + 失败开放**。探测走 `index.filter_search`（纯元数据过滤，不产生
  embedding、不花重排钱），因此放在**召回之前**而不是「召回为空再回退」：既不把一次必然为空的
  组合跑成昂贵召回，也不把 0 结果当成结论交给上层。全条件一起还有结果时一次探测即收工。
  退化写入 `degraded_reasons`（`FILTER_RELAXED:<字段>`）与 `SearchPage.relaxed`。
  **探测本身抛错时什么都不退化**：没有证据就放宽条件，等于把使用者的要求悄悄丢掉。
- [x] **8.2 抽取与判定解耦**。`lancedb_index._where` 里的 `array_has_any(...)` 等硬筛原样保留，
  新增的只是「先试硬筛、失败即退化」的决策层，没有把它们删成软排。
- [x] **8.3 生效条件**。只有「AI 解析产出」（`accepted_fields`）**且**「面板没重复手填」的字段
  才进入 `relaxable_fields`；规则链路、面板手填值、JD 下推路径一律维持现状（硬筛）。
  接口签名默认 `relaxable_fields=()`，所以 `MatchService` 等既有调用点行为零变化。
- [x] **8.4 可解释**。响应 `query_plan.relaxed_conditions` 列出退化字段；生效条件标签上加
  「已退化为排序」标记，降级提示文案把 `FILTER_RELAXED:<字段>` 翻成
  「X 条件无匹配，已改为参与排序（不再过滤）」。
- [x] **8.5 验证（已完成；含 2026-09-22 的「误伤精度」补测）**。
  `scripts/relaxation_ab_2026_09_22.py`：两条臂（`relax0` 不退化 / `relax1` 退化）除
  `relaxable_fields` 外全部同源，取数口径照 `api/search.py` 的生产路径，指标复用
  `scripts/retrieval_ablation_2026_09_20.py`；查询集三条来源——判决集
  （`.tmp-judge/queries.json`，20 个 JD × 3 问法，带 grade，可算 nDCG@10）、
  `--build`「用 `.dev-data` 真实取值拼查询」（`natural` 正常收窄 / `narrowed` 过度收窄）、
  以及为补测新增的判决集 `narrowed` 问法（必然筛空，grade 复用同一张表）。

  **实测（2026-09-22，`--build 24`，24 条 = 12 natural + 12 narrowed，产物
  `.tmp-ablation/relaxation-ab.json`）**：

  | 臂 | 空结果率 | 平均结果条数 | nDCG@10 |
  | --- | --- | --- | --- |
  | `relax0`（不退化） | **11/24 = 45.83%** | 3.38 | 不可算（拼的查询无判决标注） |
  | `relax1`（退化） | **0/24 = 0%** | 7.75 | 同上 |

  - **救回案例 11 例**（`relax0` 空、`relax1` 非空），**反向退化 0 例**。
  - 退化字段分布：`company` 10 次、`title` 4 次、`business_directions` 1 次
    （退化顺序是最不可靠的先退，公司名整串子串匹配最容易筛空）。
  - 按查询类型：`narrowed` 12 条里 `relax0` 空 8 → `relax1` 空 0（救回 8）；
    `natural` 12 条里 `relax0` 空 3 → `relax1` 空 0（救回 3）。
    `natural` 也会筛空，说明「同一个人身上取到的公司 + 职位」并不保证硬筛命中
    （公司字段在索引里的写法与 `parsed_data.current_company` 不总是一致）。
  - 判决集那一侧在**上一轮**是空转：解析预算修好后所有查询都拿到 `source=llm/mixed`
    （不再是 timeout），但 9 条小样里**没有一条查询被硬筛筛空**，两臂 20 条结果完全相同
    → 0 救回、0 反向退化。**2026-09-22 已补上可判分的查询集，见下。**

  #### 8.5 补测（2026-09-22，回答「退化是否误伤精度」）

  ##### 第一轮：补齐判分能力，测出「误伤」是真的

  两处补齐（都在 `scripts/relaxation_ab_2026_09_22.py` 里）：

  1. **判决集补一族「必然筛空」的问法**（`--phrasings standard,narrowed`）。
     判决集的 grade 是「这个候选人适不适合这个 JD」、**与查询文本无关**
     （`load_grades` 只按 jid 取表，同一 JD 的不同问法共用同一张表），所以补问法**不需要重新判分**。
     注入一条语料里不可能满足的条件，保证硬筛必然筛空、退化必然被触发。
  2. **built 集加确定性判分器**（`build_deterministic_grades()`）。原先 built 集的 qid 是
     `N000:natural`、`grades.get("N000")` 恒为 None，整批被跳过、nDCG 全是 None。
     现在按「公司/职位是否子串命中」给 2/1/0（与索引 `company_text`/`title_text` 同源，
     `search/documents.py` 就是这么抽词的）。**限制**：只覆盖公司/职位两维，方向不凑。

  第一轮（**修复前**的退化策略）读数：

  | 集 | 判分 | `relax0` 空结果 | `relax1` 空结果 | nDCG@10 | 配对（relax1 vs relax0） | 救回 | 反向退化 |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | built 24（确定性） | 公司/职位 | **10/24 = 41.67%** | **0/24 = 0%** | 0.4579 → **0.5028** | **+0.0449**（3 胜 / 0 负 / 21 平） | 10 | 0 |
  | judge 40（standard+narrowed，LLM 判分） | 判决式 | **27/40 = 67.5%** | **0/40 = 0%** | 0.0937 → **0.2113** | **+0.1177**（17 胜 / 1 负 / 22 平） | 27 | 0 |

  **退化在同一条查询上从不劣化，且显著救回空结果**——两份样本都是 0 反向退化
  （judge 侧只有 1 条 -0.0015 级别的噪声负例），nDCG 双双上升。
  这也是判决集第一次给出**带 grade 的 A/B 数字**。

  **但同一轮新增的「误伤判据」暴露了一个真问题**（`report["narrowed_vs_standard"]`）：
  拿「过度收窄查询被救回的名单」去比「同一批数据只去掉那条附加条件本该得到的名单」——
  **重合度@10 中位数 0.000**：

  | 集 | 可比样本 | 重合度@10 = 0 的条数 | 均值 | 最大 |
  | --- | --- | --- | --- | --- |
  | built 24 | 6 | **6/6** | 0.000 | 0.000 |
  | judge 40 | 9 | **8/9** | 0.111 | 1.000 |

  也就是：**救回来的那批人，与用户本意（公司/职位）一条都不重合**。judge 侧救回名单的
  `nDCG@10` 只有 0.21、`p@5` 0.26、`hits@10` 0.12 —— 数量救回来了，**问题没被回答**。

  **根因（有证据，不是猜测）**：`_RELAXATION_ORDER` 是**固定优先级 + 连环丢**
  （company → title → business_directions → career_directions → career_specializations），
  丢一条后仍空就继续丢下一条，直到剩下某个条件非空为止。而「非空」是唯一判据
  （8.1 的失败开放），**不含任何相关性判据**。实测一次丢 2~3 条的案例：
  `J04:narrowed 丢掉 title、career_directions、career_specializations`、
  `J18:narrowed 丢掉 title、career_directions、career_specializations`、
  `N009:narrowed 丢掉 company、title`。
  公司/职位恰恰是**最具体、用户最在意**的条件，而它们排在最前面被丢。

  ##### 第二轮：按 C 修完后的复测

  **候选修法 → 用户 2026-09-22 选定「C = 只丢一条 + 反转优先级」，已实现**：

  | 方案 | 做法 | 代价 |
  | --- | --- | --- |
  | **C（已采用）** | **单字段试**（只丢一条）+ **最不具体的先退**（枚举 → title → company） | 空结果率回升；多个不可满足条件时如实返回空 |
  | A | 只丢一条，但保留原优先级 | 会丢掉「本来能满足的那条」 |
  | B | 保留连环丢，只反转优先级 | 仍可能一次丢 2~3 条 |
  | D | 只改界面提示 | 问题仍在，只是不再静默 |

  实现要点（`search/service.py`）：
  - `_RELAXATION_ORDER` 反转为 `career_specializations → career_directions → business_directions
    → title → company`；
  - 判定从「按顺序连环丢、丢到非空为止」改成 **全条件先探一次（非空就直接收工）
    → 然后对每个允许退化的字段单独试「只把它拿掉能不能筛出人」→ 取第一个成立的**；
  - 一条都不成立时**什么都不动、返回空**。
  - **为什么必须单字段试而不是只把 `break` 挪个位置**：连环丢会丢掉「本来能满足的那条」——
    语料里只有「方向 = TECH_BACKEND」的人时，`[company=不存在, 方向=TECH_BACKEND]` 两条一起筛空，
    按顺序先丢方向、剩下 `[company=不存在]` 依旧空；单字段试会先发现「只丢 company」能筛出人
    （守卫：`test_single_field_probe_finds_the_culprit_instead_of_dropping_the_satisfiable_one`）。

  **修复后复测（同一批查询、同一家供应商）**：

  | 集 | `relax0` 空结果 | `relax1` 空结果 | nDCG@10 | 配对（relax1 vs relax0） | 救回 | 反向退化 | **重合度@10** |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | built 24 | 修复前 41.67% → **45.83%** | 0% → **4.17%** | 0.5028 → **0.6958** | +0.0449 → **+0.2986**（9 胜 / 0 负） | 10 | 0 | **0.000 → 0.925**（中位 1.000，7/8 完全重合） |
  | judge 40 | 67.5% → **70%** | 0% → **35%** | 0.2113 → 0.2017 | +0.1177 → +0.1022（14 胜 / 1 负） | 27 → 14 | 0 | **0.111 → 0.725**（中位 0.800，2/4 完全重合） |

  结论：**误伤基本消除，净收益保留**。
  - built 侧 `report["narrowed_vs_standard"]` 的**配对差值从 -0.5801 变成 0.0000**——救回的名单与
    「只去掉那条附加条件本该得到的名单」逐条一致；nDCG 也从 0.50 涨到 0.70（因为救回的是对的人）。
  - judge 侧差值是 **-0.0014**（≈0），重合度从 0.111 升到 0.725。
  - 代价如预期：**空结果率回升**（built 0%→4.17%、judge 0%→35%），这些是「丢哪一条都还是空」
    的诚实结果——界面会说「N 条精确条件下无匹配」，而不是给一份看起来有结果的错名单。
  - 退化字段分布也印证了策略生效：built 侧从 `company 9 / title 2` 变成
    **`business_directions 7 / career_directions 1 / company 2`**；judge 侧 company 丢的次数
    从 13 降到 5（且只在真需要时）。

  **判分注入方式也修了一处设计缺陷**：判决集的 `narrowed` 最初注入的是一对「不可能的公司+职位」
  （`impossible_pairs`），但那对组合的**每个分量各自都可满足**（拿的都是语料里真实存在的值），
  于是「只丢一条」之后会剩下「该公司的人」——既不是本意也不是空，测出来的差异来自注入方式。
  改成注入**一个语料里不存在的公司名**（`absent_companies`，按固定词表拼并逐个验证不存在），
  丢掉它之后剩下的正好是该 JD 自己的条件，读数才干净。

  另外建议**把这条判据固化成闸门**：把「救回名单 vs 对照名单的重合度」与
  「救回名单 nDCG 达到对照的多少」写进脚本的判定里，免得以后再退回「非空就算成功」。
  核对脚本：`.tmp-api/analyze_relaxation_overlap_2026_09_22.py`（读产物、与判分口径无关）。

  **上一轮跑不出结论的原因（是缺陷，不是脚本问题）**：所有查询都返回
  `source=rule / degraded=timeout`——AI 智能解析一次都没成功，于是
  `accepted_fields` 恒为空、`relaxable` 恒为空，两条臂完全一致。详见下方「AI 智能解析预算」。

  **遗留观察（未改，留待口径确认）**：24 条里仍有 2 条（`N010`、`J02:vague` 一类）
  解析吃满 20 秒预算 → `degraded=timeout` 回退规则链路；同一条查询单独隔离跑只要
  3.6~5.9 秒。差异来自批量串行时的限流重试（`manager.generate` 的链路内等待）与真实
  解析耗时叠加。这是账号配额导致的**容量**问题，不是解析预算不够。

  **AI 智能解析预算（2026-09-22 实测，用户已定口径：独立预算 20 秒）**：

  `search/parse.py:PARSE_TIMEOUT_SECONDS` 原为 2.5 秒，而实测同一段解析提示词（2797 字）：

  | 调用 | 耗时 | 思考 token | 解析出的字段 |
  | --- | --- | --- | --- |
  | 智谱 `glm-5.3-flashx` · 不发思考字段 | 5.6s | 429 | 少一个（无 career_specializations） |
  | 智谱 · `reasoning_effort=low` | **2.0s** | 0 | **最全** |
  | 智谱 · `reasoning_effort=high` | 3.0s | 112 | 同 low |
  | 智谱 · `reasoning_effort=max` | 17.0s | 1626 | 同 low |
  | 阿里 `qwen3.8-flash` · 关思考 | **3.3s** | — | 最全（7 个字段） |
  | 阿里 `qwen3.8-flash` · 开思考 | 18.6s | 651 | 少一个 |
  | 阿里 `qwen3.8-flash` · 开思考 + `effort=low` | 14.2s | 772 | 同关思考 |

  结论（回答用户「调用快慢是否和思考强度相关」）：**强相关，且几乎就是思考 token 数的线性函数**；
  对「把一句话拆成结构化字段」这种机械任务，**提高思考强度只是纯等待，输出字段基本一样**
  （`max` 17 秒与 `low` 2 秒产出相同，开思考 18.6 秒与关思考 3.3 秒产出相同甚至更少）。
  所以正确的省时手段不是等，而是**关思考优先、关不掉才压低档位**：
  阿里 3.3s（关思考）、智谱 11.2s → **2.0s（`low`）**。

  待办 1–4 **已全部完成**：
  1. ✅ 解析预算独立：`parse_enabled` 时请求总预算 = 解析预算（20s）+ 检索预算（`search_timeout`），
     解析不再与 FTS/向量/重排抢同一份预算（`api/search.py:_search_deadline_budget`）。
  2. ✅ 查询解析「关思考优先」：`runtime.py` 建 `QueryParser` 时传
     `reasoning_effort="low", prefer_off=True`；`prefer_off` 让槽位配置的强度不参与。
  3. ✅ AI 设置里逐个模型手动选思考强度（`AiConnection.reasoning_efforts`，见下方「思考强度」）。
  4. ✅ 重跑 `scripts/relaxation_ab_2026_09_22.py`，8.5 实测见上。

  **踩到的真机缺陷（`prefer_off` 的由来）**：给查询解析加 `reasoning_effort="low"` 后，
  阿里侧**所有**查询变成 `degraded=provider_error`。直连原文：

  ```
  HTTP 400 {"error":{"message":"<400> InternalError.Algo.InvalidParameter:
            'reasoning_effort' must be 'none' when 'enable_thinking' is false","type":"invalid_request_error"}}
  ```

  即 `enable_thinking=false` 与 `reasoning_effort` **互斥**。这个 400 被 `QueryParser` 的
  `except Exception` 吞成 `provider_error`，表现成「AI 智能解析永远回退规则链路」。
  两层守卫：`router._with_target_effort` 保证「明确关思考时不发强度」，
  `parameter_mapping.apply_reasoning` 里 `thinking_disabled` 再兜一次。

  **思考强度（用户 2026-09-22 定的口径）**：
  - **强度即开思考**：槽位配了强度就等于要求该模型思考，可切换思考的模型会被显式打开
    （否则就是上面那个 400）。不配强度 = 跟随模型默认，也就是最快的那条路。
  - **查询解析固定关思考优先**，不受槽位配置影响（实测 3.3 秒 vs 14.2 秒，字段一致）。
  - 档位取值只能来自模型档案声明的 `supported_reasoning_efforts`，保存前由
    `validation.validate_connection` 校验；目录外模型没有档位声明 → 一律拒绝
    （否则会出现「设了强度但请求体里没这个字段」）。


#### 实施中修正的两处（都只在真实数据下才暴露）

1. **`_SOFT_FIELDS` 从「只做排序信号」改为「可退化硬筛 + 恒有排序信号」**。
   `search/parse.py` 原先把 LLM 解析出的 title/company **完全不下推**为条件（依据是 2026-09-21
   抽检：11 条空结果有 10 条由这两个条件造成）。但按 8.1 的口径，正确做法是**两者都要**：
   既下推为硬条件（公司名写全时精度更高），又并入词条——这样退化时条件从 where 里消失，
   软排信号仍在。只下推不并词条会让「退化为软排」变成「悄悄丢掉条件」。
2. **`_hydrate_hits` 必须用实际生效的条件**。API 层在检索后还会用 SQLite 事实**再校验一遍**
   候选人，其中 `filters.company` / `filters.title` 是硬过滤。若它用的仍是入参，
   刚被放宽掉的条件会被重新当硬条件执行一遍，把结果又滤回 0——**退化静默失效，且只在真有
   数据时才暴露**。因此 `SearchPage` 新增 `effective_filters`，API 层取它做校验。


### 任务组 9（P1）供应商专项：问题 11/12/13/14/17/18

**「用 API 平台的 key 测就能覆盖订阅平台」这个口径的边界（2026-09-22 勘察结论）**：

目录里订阅平台是**独立的 provider 条目**，与 API 平台并列（`provider_catalog.builtin.json`）：

| provider | 参数口径 | base_url | 预置模型 |
| --- | --- | --- | --- |
| `kimi_open` | `kimi_open` | `api.moonshot.cn/v1` | `kimi-k2.6`、`kimi-k3` |
| `kimi_code`（订阅） | `kimi_code` | `api.kimi.com/coding/v1` | `k3` |
| `qwen` | `qwen` | `dashscope.../compatible-mode/v1` | `qwen3.8-flash`、`qwen3.8-max` |
| `qwen_code`（订阅） | `qwen` | `coding.dashscope.aliyuncs.com/v1` | `qwen3.7-plus` |
| `zhipu` | `zhipu` | `open.bigmodel.cn/api/paas/v4` | `glm-4.7-flashx`、`glm-5.3`、`glm-4v-flash` |
| `zhipu_code`（订阅） | `zhipu` | `open.bigmodel.cn/api/coding/paas/v4` | `glm-5.3` |

- **「请求体一致」成立**：`qwen_code` / `zhipu_code` 直接复用 API 平台的 parameter_style；
  `kimi_code` 虽是独立 style 名，但 `parameter_mapping.apply_reasoning` 里 `kimi_open` 与 `kimi_code`
  走**完全相同**的分支（`thinking: {type: ...}` + `reasoning_effort`）。所以差异确实只在 key 与 url。
- **但「预设相同模型」不成立**：订阅平台的模型名与 API 平台普遍**不同**（`k3` vs `kimi-k3`、
  `qwen3.7-plus` vs `qwen3.8-max`），唯一真同名的是智谱两边的 `glm-5.3`。
  模型档案也不总一致：`k3` 声明 `supports_temperature: true`，`kimi-k3` 声明 `false`
  ——这是请求体会**真的不同**的地方（temperature 发不发）。
- 结论：用 API 平台测能覆盖**参数映射 / 鉴权 / JSON schema / 超时 / 错误分类**这条链路，
  但**不能**证明订阅平台那个具体模型 ID 可用、也不能证明它的能力档案正确。
  这条边界必须写进界面提示（目录已有 `subscription_warning` 字段可承载），不能默认两者等价。

- [x] **9.1 通义千问 ✅ 复测达标**（修完探测后）。检测 **47.2s → 4.2s**，矩阵**全绿**
  （auth/text/json/reasoning/vision）；5 份夹具 5/5 READY，单份 10~70s。
  原「不达标」是**探测链路误判**：检测把 text 判成不可用 → 绑定时只剩 vision 角色 →
  解析链路缺 fast_text → 5 秒内失败。
- [x] **9.2 硅基流动 ❌ 定性完成（是真实缺陷，不是探测问题）**。检测 8.1s、矩阵全绿，
  但 5/5 全 `E_PARSE_INCOMPLETE`。用 `.tmp-api/diag_siliconflow_parse_2026_09_22.py`
  把判据拆开看（关键：`pipeline.py:166` 在完整性检查**之前**就写 `review_data`，
  所以不合格时 `parsed_data` 恒为空，证据只在 `review_data`）：

  ```
  模型实际只回出 3 个字段：name="胡凯"、total_years=8.0、summary=""（空串）
  其余 39 个字段全 null / []
  信号数 = 2（门槛 3）   内容信号 = 0/3（门槛 2）
  ```

  即 `deepseek-ai/DeepSeek-V4-Flash` 做简历结构化时**只抽出姓名和年限**，
  技能 / 工作经历 / 总结 / 学历 / 城市全空。这不是「差一点」，是基本没抽出内容。
- [x] **9.4 火山引擎多模型 ✅ 达标**。检测 46.3s、5/5 READY，但**单份 150~250 秒**。
- [x] **9.5 智谱 ✅ 达标**。检测 46.2s、5/5 READY、分点 3~5 无碎片、单份 20~40s。

**探测路径的四处修复（本轮真机验收暴露，静态分析全都看不出来）**：

1. **超时被误报成网络错误**（`openai_chat.py`）。`httpx.TimeoutException` 是
   `httpx.RequestError` 的子类，原代码只有后者分支 → 读超时被报成 `E_API_NETWORK`
   「无法连接 API 服务」，使用者会去查代理/DNS。已拆出 `E_API_TIMEOUT`（复用 HTTP 408 口径）。
2. **并发探测被限流就直接判「不可用」**（`probes.py`，本轮 3.x 并发化引入的回归）。
   已改为：限流/繁忙类失败（**快速失败**，重试几乎不花预算）串行重试一次再下定论；
   文本是重试后才通过时补跑 JSON 探测；鉴权取**重试后**的值。
3. **我一度把首发预算从 45s 砍到 30s，反而制造了新回归**。实测阿里 `qwen3.8-flash`
   关思考后做文本探测要 **34.9 秒**，30 秒正好裁掉它（阿里、硅基双双从「通过」变「超时」）。
   已撤回：45 秒是首发与重试**共用**的，不切块留给重试 —— 需要重试的都是快速失败，
   首发根本花不了几秒。
4. **探测请求没有输出长度上界**（也是「慢」的真正主因之一）。直连实测同一模型：
   `qwen3.8-flash` 不带 thinking 字段 **73.1s** / `enable_thinking=False` **34.9s** /
   带 thinking **65.7s**，而同模型 vision 只要 **2.6~5.4s** —— 差别在**输出长度**
   （vision 只回一句）。全仓库 grep 确认**从未发过 `max_tokens`**，所以生成长度完全自由、
   延迟不可控。已在探测提示词里显式要求「只回一句话」，检测时间随之从 47.2s 降到 4.2s。
   > 业务调用（简历/JD 解析）不受此影响：它们有 JSON schema 约束输出形状；
   > 但「无 max_tokens」本身仍是个待评估项（见 9.9）。

**关于「批量解析慢是不是并发额度限制」的实测结论（用户提问）**：

- **应用侧本来就是 8 路并发**（`runtime.py:811` 起 8 个 `_worker_loop`），
  验收脚本里的「5 份 × 200 秒」是脚本逐份等待造成的串行，不代表应用行为。
- **并发只让阿里慢约 10%**（单发 73.1s → 并发 77.2/87.2s），**不是并发额度限制**。
  真正的慢来自**输出长度无上界**（见修复 4）。
- **配额限制确实存在，但只在 Kimi**：直连拿到
  `request reached organization max RPM: 3` —— 该账号 RPM=3，而检测并发首发恰好 3 个请求。
- **火山不是额度问题**：检测全绿、零 429，纯模型慢。
- **解析/画像链路完全没有 pacing**（pacing 只做在检索链路的 `HybridSearchService._pace`），
  对低 RPM 账号 8 路并发是负收益 → 见 9.8。
- [x] **9.9 给文本类业务调用加输出上界 ✅ 已实现（评估结论见下）**。
  `GenerationRequest.max_tokens` + 适配器按需发送；`profile_spec.PROFILE_REWRITE_MAX_TOKENS = 1024`
  用在两侧画像的**定点重写**（`_repair`，文本输出）上。

  **评估结论：只给文本类调用设上界，结构化调用一个字段都不发。** 依据：
  - 输出长度确实是延迟主因（同模型「只回一句」2.6~5.4s vs 不约束文本 73s），所以值得设；
  - 但结构化调用被截断会变成 `E_API_SCHEMA`（JSON 不完整），**比慢更糟**，那边由 schema 兜；
  - 取值是**安全网**而非约束：画像上限 160 字 ≈ 240 token，取 1024 留约 4 倍余量，
    合规输出永远不会被截断。
  - 判别式：`test_max_tokens_only_sent_when_requested`（不给就不发）、
    `test_complete_text_forwards_max_tokens_and_defaults_to_none`（一路传到请求）。
- [x] **9.8 生成链路的并发与限流自适应 ✅ 已实现**。
  新增 `providers/ai/pacing.py:GenerationPacer`（令牌桶 + 在飞上限 + AIMD 自适应），
  接在 **`AiProviderManager.generate()`** 上——它是**所有**生成调用（解析/画像/BD/复核）的
  唯一入口，接这一处就全覆盖，不必逐个调用方改。

  为什么是自适应而不是写死 RPM：使用者插的是自己的 Key，配额各不相同（实测 Kimi 该账号
  3 RPM，而检索链路原来硬编码 48 RPM），写死要么对高配额账号拖后腿、要么对低配额账号没用。
  现在只依据对端反馈调速：限流/繁忙 → **速率折半**（下限 3 RPM）并按 `Retry-After` 静默；
  成功 → 每次 +5%（上限 600 RPM）。即降得快、升得慢，避免在限流边界震荡。
  在飞上限默认 8，与 worker 数对齐：真正的约束交给速率，而不是把并发压小去惩罚所有供应商。
  `THROTTLE_ERROR_CODES` 收敛为唯一定义（探测的串行重试集合也引用它）。

  写这个测试时又暴露一个盲点（已一并修掉）：**不能只看外层错误码**。单供应商（最常见情形）
  在全部目标失败后，路由抛的是聚合错误 `E_AI_ALL_PROVIDERS_FAILED`，逐次尝试的真实错误码
  （`E_API_RATE_LIMIT`）只在 `details` 里——只看外层等于永远检测不到限流。
  `_throttled_retry_after()` 因此同时检查外层码与 `details` 条目。
  另外该测试还顺带证明了另一条既有传导链：**探测被限流挡住 → `probed_roles` 为空 →
  绑定后没有可用目标 → 请求在路由层就变成 `E_AI_NO_PROVIDER`**，与阿里/硅基那次失败同源。

- [x] **9.6 订阅平台口径落地**。见上方「订阅平台口径」表；目录 `version 7 → 8`，
  把「模型名/能力档案可能不同、未实测」写进三个订阅 provider 的 `subscription_warning`
  （向导会据此要求用户勾选确认），并补两条**离线**断言：
  订阅 style 与按量计费 style 在**同一模型档案**下写出逐字段相同的请求体
  （`test_subscription_style_writes_the_same_body_as_api_platform`），
  以及两个订阅 provider 直接复用按量计费的 `parameter_style`
  （`test_subscription_providers_reuse_their_api_platform_parameter_style`）。
- [x] **9.7 低 RPM 供应商的探测口径 → 按用户口径不处理（关闭）**。把「限流导致未能判定」
  从「不可用」里分出来（三态）会动能力矩阵结构与界面；对已知低 RPM 的供应商串行发起 +
  按窗口等待会拉长检测时间。用户 2026-09-22 决定**暂不处理**，保留现状。
  缓解措施已在 9.17：探测的「误报不可用」只算**告警**，不再把成功的轮次判成不达标。
  > 复现路径仍然有效：Kimi 开放平台该账号 RPM=3，检测并发首发恰好 3 个请求 → 必然吃满配额。
  > 若以后要处理，优先做「探测首发串行 + 一个 `Retry-After` 窗口」，不要改矩阵结构。

**预设模型变更（用户 2026-09-22 指定，目录 version 7 → 8）**：

| provider | 快速 | 思考 | 视觉 |
| --- | --- | --- | --- |
| `zhipu` | `glm-5.3-flashx` | `glm-5.3` | `glm-5.3-flash` |
| `zhipu_code` | `glm-5.3-flash` | `glm-5.3` | `glm-5.3-flash` |
| `qwen` / `qwen_code` | `qwen3.8-flash` | `qwen3.8-max` | `qwen3.8-flash` |
| `kimi_code` | `kimi-for-coding` | `k3` | `k3` |

- `qwen` 原本就已符合；其余按用户 2026-09-22 指定修改（旧模型条目保留为可选，避免让既有用户配置失效）。
- **智谱两侧不同是当日真机复测后的修订**：开放平台有 `glm-5.3-flashx`，Coding Plan 没有更低的快速
  模型，只能用 `glm-5.3-flash`（用户口径）。
- 守卫测试：`test_zhipu_and_qwen_presets_match_the_coding_plan_model_names`、
  `test_kimi_code_subscription_uses_the_coding_endpoint_model_ids`。

**预设模型的模型 ID 真机核对（2026-09-22）**：

`.tmp-api/diag_model_ids_2026_09_22.py` 逐家拉 `/models` 列表比对用户指定的模型名：

| provider | 模型 | 是否在 `/models` 列表里 |
| --- | --- | --- |
| `zhipu` | `glm-5.3-flash` / `glm-5.3` | ✅ 都在（共 11 个模型） |
| `qwen` | `qwen3.8-flash` / `qwen3.8-max` | ✅ 都在（共 261 个） |
| `kimi_open` | `kimi-k2.6` / `kimi-k3` | ✅ 都在 |
| `kimi_open` | `kimi-for-coding` / `k3` | ❌ 不在 —— **这是预期的**：这两个是 `kimi_code` 订阅端点的模型名，开放平台不认 |

即「模型名写错」这一假设被排除：`glm-5.3-flash` 确实存在。

- [x] **9.10 智谱 `glm-5.3-flash` 是「始终思考」模型 ✅ 已修（真机验收抓到）**。
  预设换成 `glm-5.3-flash` 后，检测矩阵里 `auth` / `text` / `json` / `vision` **全部**变成
  `E_API_FORMAT`，只有思考角色 `glm-5.3` 通过。直连复现拿到决定性原文
  （`.tmp-api/diag_zhipu_flash_2026_09_22.py`，逐项剥离请求体）：

  ```
  最小体                     HTTP 200
  带 temperature             HTTP 200
  带 thinking=disabled       HTTP 400 {"error":{"code":"1210",
                             "message":"该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"}}
  带 thinking=enabled        HTTP 200
  带 json_object/json_schema HTTP 200
  ```

  原因是**档案声明错了**：`glm-5.3-flash` 支持 `off` 被误写进
  `supported_reasoning_modes`，于是 `apply_reasoning` 在快速槽位（`reasoning=OFF`）
  发出 `thinking: {"type": "disabled"}`，对端直接 400。**与模型名无关**——
  直连不关思考时它的文本与视觉都是 200（视觉实测能正确识别内置 64×64 测试图）。
  修法是把两个 provider（`zhipu` / `zhipu_code`）的档案改成
  `supported_reasoning_modes: ["auto","required"]`、efforts 补上 `max`。
  守卫测试：`test_glm_5_3_flash_is_always_thinking`（档案层）+
  `test_always_thinking_preset_gets_no_reasoning_toggle`（请求体层：快速槽位下请求体必须为空）。

  **验收复测**：检测 **19.9s、矩阵五项全绿**；批量解析 5/5 全走远程 AI。

- [x] **9.11 正文关键词会覆盖状态码，把限流误判成输入错误 ✅ 已修（火山验收抓到）**。
  火山方舟文本探测报 `E_API_INPUT`（「请求超出限制或格式不支持」），而思考/视觉都通过。
  直连拿到原文（`.tmp-api/diag_volcano_probe_2026_09_22.py`）：

  ```
  HTTP 429 {"error":{"code":"SetLimitExceeded",
            "message":"Your account [...] has reached the set inference limit for the
                       [deepseek-v4-flash-ga] model, and the model service has been paused.
                       ... Exceeded ...","type":"TooManyRequests"}}
  ```

  `map_http_error` **先看正文关键词、后看状态码**，这句里的 "Exceeded" 命中输入类关键词
  `exceed`，于是 429 被归成 `E_API_INPUT`（输入类、不可重试、不可切换）。后果不是
  「报错难懂」而是**静默降级**：

  ```
  探测不认它是限流 → 不走限流串行重试 → 文本能力判不可用 → 连接没有 FAST_TEXT 角色
  → 解析回退本地确定性解析（0.2~3.6 秒「成功」，一次模型调用都没发生）
  → 界面显示解析完成、画像为空 → 被误读成「该供应商解析质量差」
  ```

  同样的误判也会让生成链路的 AIMD 限速器永远认不出限流、永不降速。
  修法：`_BODY_HINT_EXCLUDED_STATUSES`（401/402/403/408/429/5xx）**状态码已自证结论时
  不再用正文改写分类**；400/404/422 等仍有歧义的状态照旧用正文细化。
  守卫测试：`test_throttle_and_server_bodies_do_not_override_the_status_code`。

  > 火山这一轮的**事实结论**：该账号的 `deepseek-v4-flash-ga` 触发了火山控制台的
  > 「安全体验模式」推理额度上限，服务被暂停，需要在模型激活页调整或关闭该模式——
  > 这是**账号侧限制**，不是代码缺陷（同一账号的 `deepseek-v4-pro-ga` 仍返回 200）。

- [x] **9.12 本地兜底解析不再静默 ✅ 已落地**。
  `_RoutedResumeParser` 在 `E_AI_NO_PROVIDER` 时回退 `LocalResumeParser`——这条兜底本身是
  设计（AI 智能解析默认关闭的用户要走它），但静默就变成上面那种误导。
  现在把**实际路线**写进抽取诊断：`revision.extraction_diagnostics["ai_parse_route"]`
  取 `"remote"` / `"local"`（`providers/factory.py:uses_remote_ai()` → `resumes/pipeline.py`），
  该字段已由 `api/resumes.py` 暴露给前端，`ResumeReviewDrawer` 在 `local` 时显示提示
  「本次未使用 AI：当前没有可用的快速解析服务……请到「AI 设置」检查供应商配置与检测结果」。
  守卫测试：`test_routed_parser_reports_when_it_will_fall_back_to_local`、
  `test_pipeline_records_which_parse_route_was_used`（含「不认识的解析器按 remote 记录」的失败开放分支）。

- [x] **9.13 验收脚本口径修正：等任务终态，而不是等修订状态 ✅ 已修**。
  上一轮脚本把 `ResumeRevision.status in {READY, FAILED}` 当终态，于是出现「5 秒跑完 5 份」这种
  不可能的结果。用 `.tmp-api/diag_task_timeline_2026_09_22.py` 把任务时间线拉出来对照才看清：

  - 智谱轮：3 份 `SUCCESS` 用 2.9~3.6 秒、2 份 `DEAD_LETTER` 用 0.2~0.4 秒 —— **全是本地兜底**
    （见 9.11 的因果链），根本不是「智谱解析慢/差」；
  - 火山轮：同样 0.2~3.6 秒，同一因果链；
  - 阿里轮：`SUCCESS` 用 23.2 / 70.2 / 166.0 秒（真实远程解析），另有 1 份第 2 次尝试失败后
    正要重试、1 份在 `RETRY_WAIT` 中被**提前清场**删掉（`CANCELLED(target_deleted)`）；
  - Kimi 轮：有 1 份 `attempts=4` 最终 `SUCCESS`（说明限速器+重试确实能收敛），
    但 3 份在 `RETRY_WAIT` 中被提前清场删掉 —— 「1/5」这个结论是脚本自己造成的。

  已改为：按 `PARSE_RESUME` 任务的 `SUCCESS/CANCELLED/DEAD_LETTER` 判终态并记录
  `attempts` / `parse_route` / 修订状态；**只有全部任务到终态才允许清场**；
  判定时把 `parse_route == "local"` 的结果单独列出并声明「本轮结果不反映供应商能力」。

- [x] **9.14 智谱快速模型的真实可用性：`glm-5.3-flash` 不可用、`glm-5.3-flashx` 可用 ✅ 已改**。
  修完 9.10 后检测全绿（19.9s），但真实解析「成功了却没有画像」。用
  `.tmp-api/diag_parse_one_2026_09_22.py` 把单份解析的 `review_data` 原样打出来：

  ```
  name     = "声明：该人选信息仅供公司招聘使用，严禁以招聘以外的任何目的使用人选信息..."（免责声明）
  summary  = 整篇原文（含 6 遍重复的猎聘样板）
  experiences = []  educations = []  ai_profile_summary = null  ai_profile_points = []
  字段非空数：10/42
  ```

  再用 `.tmp-api/diag_zhipu_schema_2026_09_22.py` 直接对比（同一份 7919 字原文 + 42 字段 schema，
  只换模型与是否关思考）：

  | 模型 | 请求体 | 结果 |
  | --- | --- | --- |
  | `glm-5.3-flash` | 关思考 | **HTTP 400**（始终思考，不支持关闭） |
  | `glm-5.3-flash` | 不关思考 | **>300 秒读超时**（3/3 超时） |
  | `glm-5.3-flashx` | 关思考 | **HTTP 400**（同样始终思考） |
  | `glm-5.3-flashx` | 不关思考 | **HTTP 200 / 63.7 秒**，`name=陶蕾`、`experiences=3`、`points=5` ✅ |
  | `glm-4.7-flashx` | 关思考 | **HTTP 200 / 19.7 秒**（最快） |
  | `glm-4.7-flashx` | 不关思考 | **>300 秒读超时** |

  结论：**智谱的「快速」槽位必须用「可关思考」的模型**，否则它就是最慢的那个。
  `glm-5.3-flash` 因此从 `zhipu` 的推荐里撤下，换成 `glm-5.3-flashx`（63.7 秒、结构完整）；
  `zhipu_code`（Coding Plan）没有更低的快速模型，按用户口径保留 `glm-5.3-flash`，
  并已知它会显著偏慢（这是订阅套餐的模型供给限制，不是代码缺陷）。

- [x] **9.15 限速器冷启动突发改为 1 ✅ 已改**。
  原先「突发额度 = 在飞上限」= 8，等于冷启动瞬间放出 8 个请求。实测智谱 5 份批量解析的
  **第一次尝试全部**吃 429（每份任务的 `attempts` 都变成 2，靠任务重试才成功），Kimi 那种
  3 RPM 的账号更糟——这与「限速是为了别撞限流」的目标正好相反。改为 `DEFAULT_BURST = 1`
  （可通过构造参数显式调大），代价只是冷启动时相邻请求间隔 1 秒（初始 60 RPM），
  成功后每次 +5% 很快恢复。守卫测试：`test_cold_start_burst_is_one_not_the_in_flight_cap`。

- [x] **9.16 限流在生成链路内部消化（用户 2026-09-22 选定）✅ 已实现**。
  原先 429 直接上抛给任务层，而任务重试间隔是 1s / 5s / 30s / 2min / 10min，与限流窗口
  （实测 Kimi 该账号 3 RPM = 20 秒）根本不匹配：5 次尝试全落在同一个窗口里耗尽，
  整批判失败。现在 `AiProviderManager.generate()` 在链路内部按限速器节奏等待后重试
  （`_MAX_THROTTLE_RETRIES = 2`，最后一次照实抛出），低配额账号表现为「慢但成功」。

  实现时踩到并解决的两个点：

  1. **只消化 429，不消化 503**。503（`E_API_BUSY`）的含义是「对端临时繁忙」，路由的重试与
     熔断已覆盖它；而且第一次失败后熔断往往已打开，紧接着重试只会拿到 `E_AI_NO_PROVIDER`，
     把真实的 503 掩盖成更含糊的「没有可用服务」——`test_failover_e2e.py` 里「两边都 503」
     那条就是这样被测试抓出来的。故新增更窄的 `_ABSORBED_THROTTLE_CODES = {E_API_RATE_LIMIT}`，
     限速器降速仍用较宽的 `THROTTLE_ERROR_CODES`。
  2. **等待必须覆盖熔断的剩余冷却**。一次 429 会同时触发熔断（连接级冷却 60 秒，
     对端给了 Retry-After 就按它的值）与限速器降速；只等限速器的话，重试还没发出去
     就撞上「熔断打开」→ 路由选不出目标 → `E_AI_NO_PROVIDER`。现取
     `max(Retry-After, 429 造成的熔断剩余冷却)` 作为静默期。

  顺带把限速器等待吃光预算的情况从裸 `TimeoutError` 改成 `ProviderError(E_AI_DEADLINE)`：
  否则它会逃到只捕获 `ProviderError` 的调用方，被当成未知异常。
  守卫测试：`test_a_transient_throttle_is_absorbed_inside_the_generation_link`。

- [x] **9.17 验收判定口径修正：探测的「误报不可用」不是不达标 ✅ 已改**。
  原判据「检测称视觉不可用，但 PDF 仍解析成功」会把**成功**的轮次判成不达标。带文本层的
  PDF 本来就走文本路径、不需要视觉角色；探测那一项不可用只是并发首发被限流（9.7，用户已决定
  暂不处理）。现改为**告警**，只有「检测说可用、真实却解析不了」才算缺陷。

**五家供应商的最终真机结果（2026-09-22，全部为真启动应用 + 真解析 5 份夹具，每轮清场）**：

| provider | 模型（快速/思考/视觉） | 检测 | 批量墙钟 | 解析 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 智谱 | `glm-5.3-flashx` / `glm-5.3` / `glm-5.3-flash` | **9.0s 五项全绿** | **110.6s** | **5/5**，分点 5/5/5/4/5，`attempts=1` | ✅ 达标 |
| 阿里 | `qwen3.8-flash` / `qwen3.8-max` / `qwen3.8-flash` | **4.2s 五项全绿** | **125.6s**（原 365.2s） | **5/5** | 解析达标；**3/5 画像分点为空**（模型未回 `ai_profile_summary`，见下） |
| Kimi | `kimi-k2.6` / `kimi-k3` / `kimi-k2.6` | 17.1s（vision 报限流） | 256.4s | **5/5**（原 1/5），分点 4/4/5/3/4 | ✅ 解析达标（**慢但成功**，9.16 的直接收益） |
| 火山 | `deepseek-v4-flash-ga` / `…-pro-ga` / `glm-5-3-flash` | 7.0s | 60.6s | 0/5（全走本地兜底） | ⛔ **账号侧限制**：快速模型被控制台「安全体验模式」暂停（`SetLimitExceeded`），需用户调整 |
| 硅基 | `DeepSeek-V4-Flash` / `…-Pro` / `GLM-4.5V` | 5.0s | 65.4s | 0/5（`E_PARSE_INCOMPLETE`） | ⛔ **真实缺陷**：模型只抽出姓名与年限（3 个字段），技能/经历/画像全空（9.2 已定性） |

- **9.16 的效果对比（同一账号、同一批夹具）**：Kimi 从「1/5 成功、4 份 `E_AI_ALL_PROVIDERS_FAILED`」
  变成「5/5 成功」，代价是墙钟从 135s 涨到 256s —— 这正是「慢但成功」而不是「失败」。
- **9.15 的效果**：智谱与阿里这一轮**全部 `attempts=1`**（上一轮智谱 5 份全部 `attempts=2`），
  冷启动不再齐发、第一次尝试就不再吃 429。
- **阿里的遗留观察**：5/5 解析成功，但 3 份的 `ai_profile_summary` 为空、1 份只有 1 条分点。
  直连看单份 `review_data` 确认是 **`qwen3.8-flash` 自己没回这两个字段**（同一份数据里
  姓名/学历/技能都正常），不是被校验丢掉，也不是本轮任何改动引入（本轮改动不触及解析请求体）。
  属于供应商侧的输出稳定性问题，记录为已知项。

- [x] **9.18 sidecar 打包链路的形态不一致 ✅ 已按用户口径修**。
  勘察发现三处互相矛盾，谁都跑不通：

  | 位置 | 期望的形态 |
  | --- | --- |
  | `backend/packaging/kerui_recruit.spec` | **onedir**（`COLLECT`，docstring 写明「避免每次启动解压」） |
  | `desktop/src-tauri/src/lib.rs:sidecar_candidates` | **两种都支持**，含 onedir 分支 `资源目录\<名>\<名>` |
  | `build_sidecar.ps1` | 单文件（要求 `dist\kerui-recruit-sidecar.exe` 是文件），且硬依赖本机不存在的 `.venv` |
  | `build_sidecar_macos.sh` | 单文件（`-x` 校验 + `cp 单文件`，对目录会直接失败） |
  | 两个平台配置 | Windows `resources` 与 macOS `externalBin` 都指向单文件 |

  磁盘上的痕迹也印证了割裂：`dist\` 里同时躺着 9-13 的 177MB onefile 旧文件与今天 08:30 的
  onedir 目录；`binaries\kerui-recruit-sidecar.exe` 实际是**同名目录**（8:31）。

  **用户口径（2026-09-22）**：Windows 走 onedir、macOS 保持 onefile。据此：

  - `kerui_recruit.spec` 改为按平台分叉（`os.name == "nt"` → `COLLECT` 出目录；
    否则 → 单文件 `EXE`）。Analysis / datas / hiddenimports 完全共用，不拆两个 spec。
  - `build_sidecar.ps1` 改为拷**整个目录**成同名目录（匹配 `sidecar_candidates` 的 onedir 分支），
    并允许在 `.venv` 不存在时退回 `py -3.12`。
  - `build_sidecar_macos.sh` 与 macOS 的 `externalBin` **无需改动**（onefile 口径不变）。

### 任务组 9.5（新）任务链：僵尸任务与确定性重试（本轮普查发现的真实缺陷）

`.tmp-api/census_deadletter_2026_09_22.py` 对 128 条 DEAD_LETTER 做了归因：

```
    56  僵尸任务：对象已删除（Resume revision not found）   ← 44%
    25  其它：API 返回内容不符合结构要求
    15  真·解析不完整
    11  其它：请求超出限制或格式不支持
    10  其它：JD is not eligible for matching
     4  其它：原文存在较多内容，但结构化结果几乎为空
     2  其它：主服务和备用服务当前均不可用
```

**这直接纠正了计划 §1.5 的前提**：那里记的「103 条 PARSE_RESUME DEAD_LETTER，规律很干净」
被当成解析质量差的证据，但实际**近一半是僵尸任务**——候选人删掉后，队列里/重试中的
`PARSE_RESUME` 还会被领到、发现对象不存在、按 `max_attempts` 重试满 5 次才进死信。
既污染指标，也白占 worker。

- [x] **9.5.1 删除时取消引用该对象的未终态任务**（根因修复）。
  `CandidateDeletionService._cancel_pending_tasks()` 在删除事务内按 **payload 里的值**
  （递归扫标量，不按固定键名——各任务类型载荷结构不同）匹配候选人与版本 id，置 `CANCELLED`。
  用 `CANCELLED` 而不是 `DEAD_LETTER`：这不是失败，是「活已经不用干了」。
- [x] **9.5.2 确定性判定不再重试**。`TaskRepository.fail(..., retryable=)` +
  `worker._NON_RETRYABLE_CODES`（当前只收 `E_PARSE_INCOMPLETE` / `E_STRUCTURED_EMPTY`）：
  内容级判定换多少次时机都一样，再试只是把同一次模型调用再烧 4 遍。实测就有
  `attempts=2` 还挂在 `RETRY_WAIT` 的 `E_PARSE_INCOMPLETE`。供应商侧错误（如
  `E_AI_ALL_PROVIDERS_FAILED`）仍照常重试——重试的用武之地在那里。
- [x] **9.3 Kimi 开放平台 ✅ 已定位根因（两个真实缺陷 + 一个配额事实）**。
  验收脚本 `.tmp-api/provider_acceptance_2026_09_22.py`（真启动 app + 真解析 5 份夹具 + 每轮清场）。
  第一轮：**检测矩阵说 reasoning/vision 不可用（E_API_RATE_LIMIT），但 5/5 简历全部 READY
  （含 2 份 PDF，必须走视觉）**——矩阵与真实结果矛盾，报"不达标"。
  原始接口直连（`.tmp-api/diag_kimi_raw_2026_09_22.py`）拿到了决定性证据：

  ```
  Your account ... request reached organization max RPM: 3, please try again after 1 seconds
  ```

  **该账号的 RPM 上限是 3**。而检测并发首发恰好就是 3 个请求 → 必然吃满配额 → 后续全 429。
  文本/思考/视觉在配额未满时都正常返回 200，所以这不是能力问题，是配额与探测方式的冲突。

  由此暴露并修复的两个缺陷（都是本次验收才暴露的，前一轮静态分析没看出来）：

  1. **超时被误报成网络错误**（`providers/ai/openai_chat.py`）。`httpx.TimeoutException`
     是 `httpx.RequestError` 的子类，而捕获分支只有后者 → 读超时被报成
     `E_API_NETWORK`「无法连接 API 服务」。使用者会去查代理/DNS，真实原因是对端太慢。
     智谱 glm-5.3（思考模型）的探测正是这样被误报的。已拆出 `E_API_TIMEOUT`
     （复用 `errors.py` 里 HTTP 408 的既有口径，不新造码），并补 2 条测试
     （读超时归 timeout / 连不上仍归 network）。
  2. **并发探测被限流时直接判"不可用"**（`providers/ai/probes.py`，本轮 3.x 并发化引入的回归）。
     已改为：首发阶段只许用 `45 - 15 = 30` 秒，把尾巴留给**串行重试**；限流/繁忙/超时类
     失败（`E_API_RATE_LIMIT` / `E_API_BUSY` / `E_API_TIMEOUT`）串行重试一次再下定论；
     文本是重试后才通过时补跑 JSON 探测；鉴权取**重试后**的值。补 2 条测试
     （限流后重试成功 → 矩阵应为可用；限流持续 → 仍如实报不可用）。
  3. 第二轮验收又暴露一个修复缺口：首发把 45 秒预算**全部吃光**时，"剩余预算不足"的闸门
     让重试一次都跑不了（日志有 `检测遇限流但剩余预算不足，不再串行重试：reasoning`）。
     已加 `_RETRY_RESERVE_SECONDS = 15.0` 预留切片。

  **待你定口径的遗留问题**：低 RPM 账号（Kimi 是 3 RPM）下，探测已经**无法**在 45 秒内
  得出可信结论——串行重试也要等一个窗口。现在的矩阵只有"可用/不可用"两态，
  会把"限流导致测不出来"显示成红色的"不可用"，这是同一类误导。
  候选做法见下方 9.7。

- [x] **9.5 智谱 ✅ 达标（解析侧）**：检测 46.2s、矩阵 auth/text/json/vision ✅（reasoning 为
  被误报的超时，修完应转可用）；`测试数据/resumes_batch` 5/5 READY，分点 3~5 条、无 ≤12 字碎片，
  单份 20.2~40.0s。
- [x] **9.7 低 RPM 供应商的探测口径 → 按用户口径不处理（关闭，同上方 9.7 条目）**：
  三态拆分要动能力矩阵与界面，串行探测会拉长检测时间；用户 2026-09-22 决定暂不处理。
  现在的缓解是 9.17 把「探测误报不可用」降级为告警，而不是判不达标。

### 任务组 10（P0）端到端验收：问题 15/16/19

见 §6。执行进展：

- [x] **10.1 三套测试全绿（2026-09-22，含 7.4 / 7.6 收口后的最终一轮）**
  - 后端全量 pytest：**1521 passed / 3 skipped**（3 条 skip 是需要真实密钥/私有 PDF 的本地验收）
  - 前端 vitest：**170 passed / 14 files**
  - 前端 `tsc --noEmit`：干净
  - Playwright（真启用隔离 sidecar + Vite）：**16 passed**
- [x] **10.2 接口功能探针**：`scripts/api_inventory_2026_09_21.py` 重新导出路由清单（**149 条**），
  再用 `scripts/api_functional_test_2026_09_21.py` 在真实 `.dev-data` 上真启动应用逐条打接口。
  中间一轮：**pass=169 / fail=1 / error=0 / 未覆盖路由=0**（170 条探针），
  唯一那条 fail 就是下面 10.4（已修）。
  **最终一轮（7.4 / 7.6 收口后，全部改动到位）：pass=170 / fail=0 / error=0 / 未覆盖路由=0**
  ——此前那条 fail 是 `jd.regen_profile` 的 504，见下方「后续更新」。
  > 本轮第一次跑之前踩了个坑：`.dev-data` 里生效的 AI 连接是上一轮验收最后留下的**硅基流动**
  > （已定性为坏模型），AI 相关接口只能等超时，探针跑了 45 分钟还没结束、结果也是被污染的。
  > 已改用 `.tmp-api/bind_provider_2026_09_22.py zhipu` 把可用供应商覆盖绑定后再跑。
  >
  > 另外探针本身也修了三处：`bulk_download` 的中文摘要进 HTTP 头导致整批 500、
  > 未覆盖的沟通记录接口（补 3 条探针，含按中段片段查证明 like 语义）、
  > `jd.regen_profile` 的 504 按用户口径纳入期望值。
  >
  > **后续更新**：7.6 把岗位画像改走快速档后，`jd.regen_profile` 的 504 **不再出现**（原因见
  > 7.6：阿里思考档 150 秒未返回）。改完后定向复跑 `--only jd,resumes`：
  > **pass=38 / fail=0 / error=0 / 未覆盖=0**，`jd.regen_profile` 返回 200。
  > 7.4 的 SSE 走的是**同一条路径的内容协商**、没有新增路由，所以路由清单与覆盖率闸门都不受影响。
- [x] **10.3 重新打包安装包 ✅ 已完成并冒烟通过**
  - `backend/packaging/build_sidecar.ps1` → PyInstaller（系统 `py -3.12`）→ Windows onedir，
    产物 `dist\kerui-recruit-sidecar\`，拷成 `desktop\src-tauri\binaries\kerui-recruit-sidecar.exe\`（同名目录）。
  - 打包产物**先单独冒烟**：`health/ready` → 200；`/api/ai/catalog` → 200，
    **9 个供应商入口齐全**，且 `zhipu` 推荐模型已是 `glm-5.3-flashx`、`zhipu_code` 仍是 `glm-5.3-flash`
    ——证明本轮目录改动确实进了安装包。
  - `npm run tauri:build:windows` → `desktop\src-tauri\target\release\bundle\nsis\recruit_0.1.0_x64-setup.exe`
    （126.6 MiB / 132,751,235 字节，
    SHA-256 `7a744ce71ba3ecfd88d6f183a491a0a831906bb3145360fa042095d18d6a00b5`）。
    **10.4 修完后重打过一次**，下表这次冒烟用的就是重打后的包。
  - **7.4 / 7.6 收口后又重打了一次（这一版才是最终产物）**：132,811,127 字节，
    SHA-256 `1c9cb52ec8e9317ffa583da6bcdff5dec5a198cf75f5fdf6609639cbc1b16333`
    （比上一版大 —— 侧车与前端产物都随本轮改动重建）。
  - `desktop/scripts/verify-windows-release.ps1 -InstallerPath <nsis exe> -RunInstallCycle`
    **隔离安装/启动/卸载冒烟通过**：
    静默安装到临时目录 → 启动应用 → **便携式数据根 `<安装目录>\data\db\recruit.sqlite3` 出现**
    （证明壳层真的拉起并跑通了 onedir sidecar）→ 静默卸载 → 用户数据仍在。
    最终一轮报告：`{ ok: true, launch_initialized_database: true, uninstall_retained_data: true }`。

  为让这条冒烟真的能跑，顺带修了三处**从未被走通**的陈旧脚本问题（CI 里没跑过这条路径）：

  1. `Process.Kill($true)`（连子进程树）只在 .NET Core / PowerShell 7+ 存在，
     Windows PowerShell 5.1 直接报 `Cannot find an overload for Kill and the argument count: 1`。
     改用两种宿主都能用的 `taskkill /T /F`（与 Rust 侧 `terminate_sidecar` 同一口径）——
     否则冒烟会留下孤儿 sidecar。
  2. 应用主程序名已随 `productName` 从 `kerui-recruit-desktop.exe` 变成 `recruit.exe`，
     脚本仍写死旧名 → 改成「候选名 + 兜底扫描」。
  3. 数据根期望写错：Windows 是**便携式**布局（`<exe 目录>\data`，
     见 `src-tauri/src/lib.rs:default_data_root`），脚本却找 `%LOCALAPPDATA%\KeRuiRecruit`
     （那只是旧版迁移来源 `legacy_data_root`）→ 造成「找不到数据库」的假失败。
     现按便携式路径检查，并**实测**卸载后数据是否仍在。

     > 踩坑记录：`build_sidecar.ps1` / `verify-windows-release.ps1` 里的中文注释会被
     > Windows PowerShell 5.1 按 GBK 误读，尾字节吃掉换行 → 报「Try 语句缺少 Catch 或 Finally」。
     > 两个脚本已写成 **UTF-8 with BOM**。注意用文本编辑工具改这两个文件后会丢 BOM，需要补回。

- [x] **10.4 `E_INTERNAL: Event loop is closed` ✅ 已按 A 方案修（用户 2026-09-22 选定）**。
  全量接口探针里 `org.import_parse` 偶发一次 500 `Event loop is closed`；单独复跑
  `--only org` 23 条全过（**间歇**）。机制：`providers/leads.py:extract` 与
  `mail/resume_gate.py` 在**同步**路径里用 `asyncio.run(...)` 驱动**全进程共享的
  httpx.AsyncClient**，而调度器又用 `asyncio.to_thread(...)` 跑它们 —— 短命循环关闭后，
  连接池里建立在旧循环上的连接再被主循环复用就抛这个错；探针**退出阶段**
  `ai_manager.close()` 的同类报错也出自这条链路。

  **修法（按循环隔离客户端，用户选定 A）**：

  - `AiProviderManager._http_client_for_current_loop()`（新增）：**第一个来认领的循环**拿到
    注入的那个客户端（生产里是主循环，测试里是注入 `MockTransport` 的那个），之后每个新循环
    各拿一份自己的；客户端表用 `weakref.WeakKeyDictionary` 以循环为键，循环回收即自动消失。
    正常路径仍然复用同一个长连接池，不受影响。
  - `OpenAIChatAdapter` 新增可选 `client_provider`，每次请求现取 —— 这样
    `leads` / `resume_gate` 以及**以后新增的任何** `asyncio.run` 调用点都一起被治好，
    不必逐个改调用链（那是 B 方案，改动面大得多）。
  - `close()` 只关注入的客户端；按循环新建的**只丢引用不关闭**——在别的循环里
    `await aclose()` 只会再触发同一句报错。
  - 顺带补上服务端证据：`main.py` 兜底异常处理器现在 `logger.exception(...)` 打 traceback
    （原先只把 `str(error)` 放进响应体，客户端侧完全看不出是哪条路径）。
  - 守卫测试：`test_http_client_is_isolated_per_event_loop`（同一循环内重复取到同一个、
    另一个短命循环拿到的必须是另一个、关停不抛错）。

---

## 3. 不做的事（明确排除，避免误伤）

- 不改「AI 智能解析」的默认值（仍是关闭）。组件 8 的软排只在开启时生效。
- 存量回填（4.7）**只允许做文本拼接**：不得调用模型重写、不得增删字、不得改顺序、不得覆盖 `ai_profile_source == "manual"` 的记录。
- 不删用户数据；验收阶段的清理只删**本轮测试夹具**，保留 AI 配置、邮箱配置、学校词表。
- 不为了「让界面显示可用」而放宽保存校验到失去意义——组件 3.5 是把真实能力**显示**出来，而不是把校验删掉。
- 密钥不写入仓库、不写入文档、不写入测试夹具。

---

## 4. 关键风险

| 风险 | 应对 |
| --- | --- |
| 放开快速槽位角色校验（3.4）后，模型被填进不支持的槽位导致调用期报错 | 参数映射必须按模型自身档案走；补「真实调用成功」的测试，而不只是「保存通过」 |
| 碎片合并（4.2）与存量回填（4.7）会改变检索用文本 | 分点是**独立向量化的子 chunk**（`documents.py:424-431`），改分点即改向量库：C 必须**跑完重建索引**（4.8）并做检索前后对比 |
| 存量回填不可逆 | 只做文本拼接（无模型、无增删字、无改序）；先 `--dry-run` 出逐条 diff 人工抽检；跑前备份 db + search；保留整体回滚点 |
| 展示层只读合并（4.6）与后端新口径不一致，同一候选人两处显示不同 | 前后端共用同一份「碎片合并规则」常量，并补一致性测试 |
| 图片分批（2.1）会影响视觉解析质量 | 分批后需抽查解析完整率；保留「一次性发送」作为可回退选项 |
| BD 助手降级到 fast_text（1.4）可能产出质量较差的线索 | 降级必须写进诊断并在界面标注，不能静默 |
| 通信记录列改表格布局（6.3）影响列宽与拖拽 | 沿用现有 `columnWidth`/`resize` 机制，补前端测试 |

---

## 5. 回滚点

每个任务组独立可回退。

会改变索引文本或文档结构的任务（4.2、4.7、6.4）需**先备份 `.dev-data/db` 与 `.dev-data/search`**，并在隔离目录验证后再应用到活跃数据根。其中存量回填（4.7）与索引重建（4.8）是一个**整体回滚单元**：还原备份 = 同时还原库与索引，不允许只还原其中一半。

---

## 6. 端到端验收方案（第 15/16/19 点）

### 6.1 达标定义（默认口径，可按你的意见调整）

**简历解析（每个供应商、5 份夹具）**
- 5/5 状态 `READY`（无 `FAILED` / `DEAD_LETTER`）；
- 5/5 过完整性门槛（不得出现 `E_PARSE_INCOMPLETE`）；
- 画像分点：无 ≤12 字碎片，分点数在 3~8 之间；
- 记录总耗时，与 DeepSeek 基线对比（慢但可接受需明确记录，不算不达标）。

**JD 解析（1 份 xlsx 批量 + 1 份 docx）**
- 全部 `READY`，`title` 非空；
- 候选人画像非空且分点合格。

**岗位画像重新生成**
- 单次生成 ≤ 90 秒（现状可达三四分钟）；
- 画像质量抽样不回退。

**人岗匹配 + AI 复核**
- 匹配结果非空、排序稳定；
- AI 复核可用（能产出结论，不报 `E_AI_NO_PROVIDERS`）。

**BD 助手**
- 能产出**非空**线索，且 `source=agent`（不是 fallback 的原始摘要）；
- 全阶段事件齐全（`planning → … → ranking → synthesized → leads → done`）；
- 失败时界面显示原因而不是「暂无线索」。

**AI 检测**
- 单次完整检测 ≤ 60 秒；
- 检测矩阵与真实解析结果一致（不再「显示可用但不解析」）；
- 思考模型填进快速槽位可正常完成一次真实解析（对应第 9 点）。

### 6.2 执行顺序（每个供应商一轮，达标后清场换下一个）

```
对每个供应商（按量付费平台，跳过订阅套餐）：
  1. 在应用内清空该连接，绑定新供应商（填入 api key / base_url / 三个角色的模型）
  2. 跑「完整检测」，记录矩阵与耗时
  3. 导入 测试数据/ 的 5 份简历 → 等全部解析 → 记录状态、不合格原因、分点质量、耗时
  4. 导入 JD导入测试.xlsx 与 JD测试.docx → 记录状态与画像
  5. 对 1 个岗位点「重新生成画像」→ 记录耗时
  6. 跑一次人岗匹配 + AI 复核 → 记录结果
  7. 跑一次 BD 助手查询 → 记录线索数与阶段事件
  8. 记录截图/日志/DB 快照作为证据
  9. 达标 → 清空本轮测试夹具（候选人 / 岗位 / BD 会话说 / 匹配结果 / 索引），保留 AI 配置与邮箱配置
     不达标 → 保留现场，归因后再测
```

`mapping测试文本.docx` 与 `新建 文本文档.txt` 用于组织架构导入与 JD 文本导入的补充回归（注意后者内容含组织汇报信息，按「JD + 组织信息混合文本」的口径验证拆分行为）。

### 6.3 覆盖范围（第 19 点）

除上面每轮的 AI 功能外，最后再做一遍**全功能端到端**：人才库检索（关键词 / 向量 / 混合三种模式、三个开关各自开关状态）、候选人详情与字段编辑、去重、导出、可迁移包、流程与提醒、看板、组织架构、映射、备份恢复、设置与首启向导。前端 vitest + Playwright 全绿，后端 pytest 全绿，接口探针零未覆盖。

---

## 7. 变更影响面（供 review 用）

| 任务组 | 后端 | 前端 | 数据/迁移 |
| --- | --- | --- | --- |
| 1 BD 助手 | `bd_agent/{agent,synthesis}.py`、`api/bd_agent.py` | `pages/BdAssistantPage.tsx`、`api/client.ts` | 无 |
| 2 视觉路径 | `providers/vision_parse.py`、`providers/errors.py`、`providers/generation_tasks.py` | — | 无 |
| 3 检测/角色 | `providers/ai/{probes,validation,catalog_models,parameter_mapping,manager}.py`、`api/ai_settings.py` | `ai/*` 面板 | 连接配置新增字段（版本化） |
| 4 画像分点 | `providers/profile_pair.py`、`providers/profile_spec.py`、`api/{resumes,jd}.py` | `components/CandidateTable.tsx`、`pages/JdManagementPage.tsx` | 存量回填脚本（C）+ **向量索引重建** |
| 5 悬停提示 | — | `pages/TalentPoolPage.tsx`、`components/ui/*` | 无 |
| 6 沟通记录 | `db/models.py`、`api/resumes.py`、`search/documents.py` | `components/CandidateTable.tsx` | **schema 迁移 19 → 20** |
| 7 画像提速 | `providers/profile_pair.py`、`jd/profile.py`、`resumes/profile.py`、`backfill/service.py` | 进度反馈 | 无 |
| 8 方向软排 | `search/parse.py`、`search/lancedb_index.py`、`match/policy.py` | 结果可解释 | 可能需重建索引 |
| 9 供应商专项 | 视归因而定 | 视归因而定 | 无 |
| 10 验收 | 复用 `scripts/` 下既有脚本模式 | Playwright | 测试夹具 |

---

## 8. 建议执行顺序

1. **任务组 1 + 2**（P0，两条主线），修完先做一次快速真机验证。
2. **任务组 3**（P0，决定后面所有供应商测试是否有意义）。
3. 跑 §6.2 的第一轮供应商验收，用结果驱动任务组 9。
4. **任务组 5 + 6**（展示类，可并行）。
5. **任务组 4**（A+B 先做，立刻见效；C 的 dry-run 结果给你过目后再正式回填 + 重建索引）。
6. **任务组 7 + 8**（性能与排序，需要 A/B 对比）。
7. **任务组 10**（全功能收口）+ 重新打包安装包。
