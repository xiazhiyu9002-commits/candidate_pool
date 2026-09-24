# 检索输入框文本解析与混合正文检索设计（2026-09-21）

## 目标

本设计解决两件事：

1. **把输入框当查询入口，而不是关键词入口。** 使用者在人才库输入框里写的是一句自然语言需求，系统应自动把它**拆解**成三份产物——硬条件（精确筛选）、词法词条（FTS）、语义查询（向量/重排）——并按使用者手选模式分别取用，同时把拆解结果**回显**出来供人工纠正。
2. **让混合检索也能检索经历/项目正文。** 关键词模式的「检索经历正文」开关在混合模式下必须具有相同含义；开关打开后，混合通道的 FTS 需要弱命中过滤，避免正文噪声进入融合池。

本设计**不**改变手动三模式（keyword / vector / hybrid）的交互：不引入自动路由，不改动现有检索与融合的排序口径（阶段 1 的弱命中过滤除外，且需 A/B 定档）。

## 已确认的决策

| 决策点 | 结论 |
| --- | --- |
| 解析引擎 | LLM 为主（一次调用产出 filters + keywords + semantic_query），规则逐字段兜底 |
| 模式选择 | 保持现状手动三选一（不新增 auto 路由） |
| 解析字段 | 全量覆盖：精确筛选的全部字段 + 关键词词条 + 语义查询 |
| AI 解析开关 | 新增独立 `parse_enabled`，**默认关闭**，三种模式均可用（keyword 模式只取用 filters + keywords） |
| 姓名/手机号/性别 | LLM 自由解析，但必须配原文 span 校验、回显可删与误判率验收 |
| 城市词典 | 扩到全国地级市 + 直辖市 + 港澳 + 「北上广深」类组合写法（2026-09-21 确认口径）；**并同时把索引侧 `location_terms` 做城市归一化**（实测 32/1475 条是「上海市 / 广东省深圳市」这类非规范写法，只改词典必然漏召） |
| 词表扩容 | 接受一次全量重投影，与其它需要重建的改动**合批**，升 `INDEX_CHUNK_VERSION`，保留旧索引回退入口。**策展口径（2026-09-21 确认，分三类）**：技能+常用缩写、岗位族进策展集（参与 `is_curated_concept` 的三处判定）；行业/领域泛词只做两侧别名展开、不进策展集（银行 ≠ 证券，当门槛会误杀） |
| 混合正文检索 | 开启，并同时给混合 FTS 加弱命中过滤 |
| 枚举类字段（职业/业务方向） | 命中枚举即**硬筛选**（与面板手选一致）；枚举不到则不产出该条件，词只留在 keywords / semantic |
| 关键词模式与 AI | 关键词模式仍不接受 `rewrite_enabled`（语义查询没有可消费的通道）；**LLM 解析不受此限制**，三模式均可用 |
| 生效条件展示 | 前端展示 `_merge_filters` 合并后的**最终生效条件**（含手填面板项），不再只展示解析结果 |
| 执行确认 | 触发索引重建前必须先提示使用者、由使用者决定是否执行；重建与画像重嵌合批，不做两次 |
| 本轮范围 | 先定稿本设计（含 2026-09-21 实测补充），**不改动任何代码**；实施分阶段推进 |

## 现状与问题

### 现有数据流

```text
用户输入（单文本框）
  → POST /api/search/candidates { query, mode, operator, rewrite_enabled, search_body, filters }
  → parse_query：正则提取 年限/学历/城市/意向城市/QS/学校等级/排除技能，其余原样作关键词
  → concepts_from_query：技能词表 + 学校别名 → LexicalConcept 组
  → _merge_filters：手填面板值优先于文本解析值
  → 按 mode 分派：空词=filter_search / keyword=FTS / vector=向量 / hybrid=FTS+向量+RRF+重排
  → query_plan 回传 parsed_conditions + retained_keywords
```

### 已确认的问题

| 编号 | 问题 | 代码事实 | 影响 |
| --- | --- | --- | --- |
| P1 | 解析只有确定性规则 | `search/query.py:parse_query` 仅覆盖年限、学历、地点、QS、学校等级、排除 | 公司、职位、学校实体、年龄、性别、海外/国内、职业方向、业务方向等面板已有字段无法从文本解析，用户必须手填 |
| P2 | 解析结果与「精确筛选」面板之间没有通道 | `api/search.py:_merge_filters` 只做单向合并（面板优先），前端不把 `parsed_conditions` 回填到面板 | 解析错了只能改输入框，不能在面板上纠正；也无法在搜索前预览将要生效的条件 |
| P3 | 词表整合能力弱 | `search/lexicon.py` 只有约 28 个技能概念 + 「后端/服务端」+ 动态学校别名 | 「数仓→数据仓库」「前端/测试/算法」这类同义与缩写无法在 FTS 内做别名 OR |
| P4 | 回显信息不可用 | `QueryPlanResponse` 只有 `parsed_conditions`/`retained_keywords`；前端只渲染条件标签 | 语义查询、保留词条、未识别片段都不可见，排障与调优无入口 |
| P5 | AI 只做改写，不做拆解 | `search/rewrite.py` 仅对齐同义词并输出单条 `semantic_query`；关键词模式被 `_validate_combinations` 明确禁止改写 | 关键词模式与筛选条件完全没有 AI 参与路径；即使开了改写，条件仍需人工手填 |
| H2' | 混合模式正文开关名存实亡 | 后端已把 `search_body` 传入 `_parallel_retrieve`（`service.py`），但前端 `client.ts` 强制 `mode === "keyword" ? searchBody : false`，且开关只在关键词模式渲染 | UI 的「检索经历正文」对混合模式不可用；`api/search.py` 字段注释「仅 mode=keyword 生效」已过时 |
| H3' | 混合通道 FTS 无弱命中过滤 | `_parallel_retrieve` 直接调 `index.search_fts`，不走 `_filter_by_hit_terms`，也无概念覆盖约束 | 打开正文后，只命中一个泛词的行会挤占 RRF 融合池与重排窗口 |

P1–P4 来自本次需求；H2'/H3' 是 `2026-09-20-vector-hybrid-retrieval-rewrite-design.md` 中 H2、H3 的未闭环部分（当时只改了后端一半）。

### 现有可复用资产

- 硬条件下推：`search/lancedb_index.py:_where` 已支持年限、年龄、学历（含包含式层级）、现居/意向地、QS、学校等级、排除技能、姓名、公司（含多值 OR）、职位、学校、海外/国内、职业大类/细分/业务方向。
- 手机号/性别：`api/search.py:_resolve_structural_candidates` 在数据库层解析候选人集合后作为 `candidate_ids` 下推。
- 学校解析：`schools/reference.py:SchoolReference.alias_groups()/resolve()` 提供标准名与别名，查询侧与索引侧同源。
- 方向词典：`direction/policy.py` 已有 `CAREER_DIRECTION_LABELS` / `SPECIALIZATION_LABELS` / `BUSINESS_DIRECTION_LABELS` 与 `extract_multi_directions`。
- 改写基础设施：`search/rewrite.py` 的缓存、校验器（长度、列表标记、硬条件新增、概念保留）与 `_RewriteOutcome` 六值映射可整体复用为解析的 semantic 分支。
- AI 装配：`providers/ai/contracts.py:TaskKind` + `runtime.py` 的 `ai_manager.task_client(...)` 模式；`bd_agent/planner.py` 是「LLM 结构化输出 + 失败回退原查询」的既有范式。

## 目标架构

```text
用户输入（自然语言）  +  手填「精确筛选」面板
        │                         │
        │            面板显式值优先（沿用 _merge_filters 语义，面板永远压过解析）
        ▼
① LLM 结构化解析（一次调用，输出三份产物）
   ├─ filters   ：覆盖 CandidateFilters 全部可解析字段（枚举白名单 + 原文 span 校验）
   ├─ keywords  ：交给 FTS 的词条（含别名/同义整合）
   └─ semantic  ：交给向量与重排的语义查询
        │  失败 / 超时 / 校验拒绝 → 逐字段回退
        ▼
② 规则兜底：parse_query（现有）产出 filters + keywords；semantic 走现有 SemanticQueryRewriter
        │  再失败 → 原词直查
        ▼
③ QueryPlan 回显：条件（来源/置信度/span） + 关键词词条 + 语义查询 + 未识别残句
        ▼
④ 按用户手选模式分派：keyword / vector / hybrid（保持现状，不自动路由）
```

设计取舍：**新增 `parse_enabled` 独立开关，不改 `rewrite_enabled` 语义。** `api/search.py:_validate_combinations` 明确禁止关键词模式带 `rewrite_enabled`；若把解析与改写合成一个开关，关键词模式将永远用不上 LLM 解析。两者同时开启时，解析产出的 `semantic_query` 取代原来的独立改写调用（一次模型调用而非两次），`rewrite_status` 语义不变。

## LLM 解析契约

### 输出 schema

新增 `TaskKind.QUERY_PARSE`，沿用 `FAST_TEXT + INTERACTIVE` 的 task client 装配方式。输出结构（Pydantic）：

```python
class ParsedSearchPlan(BaseModel):
    # 硬条件：全部字段可空，未提及则为 None / 空列表
    min_years: float | None
    max_years: float | None
    min_age: int | None
    max_age: int | None
    highest_degree: str | None          # 中文标签，如「硕士」
    degree_exact: bool = False
    locations: list[str] = []           # 现居地
    preferred_locations: list[str] = [] # 求职意向地
    max_qs_rank: int | None
    school_level: str | None            # 985 / 211 / 双一流 / 海外 / 普通
    school_region: str | None           # domestic / overseas
    school: str | None
    company: str | None
    companies: list[str] = []
    title: str | None
    name: str | None
    phone: str | None
    gender: str | None                  # 男 / 女
    exclude_skills: list[str] = []
    career_directions: list[str] = []       # 中文标签，后端反查 code
    career_specializations: list[str] = []
    business_directions: list[str] = []
    # 词法与语义
    keywords: list[str] = []            # 交给 FTS 的词条
    semantic_query: str = ""            # 交给向量与重排
```

提示词要点：只允许把原文中**已出现**的信息结构化，禁止推断补全岗位技能、行业、公司、学校或资历；未提及的字段一律留空；`keywords` 保留原文实义词的规范化写法；`semantic_query` 遵守保真规范化（沿用 `search/rewrite.py:_SYSTEM_PROMPT` 的口径，不得新增硬条件、不得输出 OR/括号/分类清单）。

### 字段映射与归一化来源

| 解析字段 | 文本表达样例 | 归一化来源 | 下推位置 |
| --- | --- | --- | --- |
| min/max_years | 3-5年 / 至少5年 / 3年以内 | 规则优先，LLM 补口语表达 | `query.py:_parse_years` 已就绪 |
| min/max_age | 35岁以下 / 30-40岁 | LLM + 区间合法性校验 | `age` 列 |
| highest_degree / degree_exact | 本科及以上 / 只要硕士 | `search/degrees.py:normalize_degree` | `highest_degree` |
| locations / preferred_locations | 现居上海 / 想去北京 | 城市词典（本轮扩容） | `location_terms` / `preferred_locations` |
| max_qs_rank / school_level | QS前100 / 985 | 规则已有 | `qs_rank` / `school_tags` |
| school_region | 海外背景 / 国内高校 | 枚举 domestic / overseas | `school_region` |
| school | 北大 / 上海交通大学 | `SchoolReference.resolve()` → 标准名 | `school_text` |
| company / companies | 字节 / 阿里或腾讯背景 | LLM，无词典 | `company_text`（多值 OR 已支持，需补 API 字段） |
| title | 后端负责人 / 做过测试开发 | LLM，无词典 | `title_text` |
| name / phone / gender | 姓名:张三 / 138… / 女生 | LLM 自由解析 + 原文 span 硬校验（高误判，见护栏） | DB 层解析 + `name_text` |
| exclude_skills | 排除外包 / 不要PHP | 规则已有 | 召回后排除 |
| career_directions / career_specializations / business_directions | 数据架构 / 风控 | `direction/policy.py` 标签反查 code | `career_*` / `business_directions` |

### 必须通过的校验

任一字段不过则**该字段回退规则值**，其余字段照常采用，不整体丢弃：

1. **原文 span 依据**：任何 filter 值必须能在原文中定位（字面或规范化别名），否则丢弃——禁止 LLM 造公司、学校、姓名。
2. **枚举白名单**：学历、学校等级、性别、`school_region`、方向 code 归一化后必须命中现成枚举，未知值丢弃并记录降级原因。
3. **区间合法性**：`min <= max`；年龄 16–80；年限 0–80（对齐 `CandidateFiltersRequest` 的 `Field` 约束）。
4. **不丢词**：原文实义词必须落在 `filters` 值 ∪ `keywords` ∪ `semantic_query` 之一；漏掉的进 `unparsed_terms` 回显。
5. **语义查询校验**：长度 ≤ 80 字符且 ≤ 原词 2 倍、无新增列表标记、不引入新硬条件、已策展概念全部保留——直接复用 `search/rewrite.py` 的既有校验器。
6. **时间预算**：解析 ≤ 2.5s（占 `HybridSearchService.search_timeout` 的小头），超时即回退，不得挤占 FTS / embedding / 重排预算。
7. **缓存**：键 = 模型身份 + `PARSE_PROMPT_VERSION` + `LEXICON_VERSION` + 规范化原文；LRU 256 / TTL 600s，沿用改写器实现。

### 枚举类字段（方向）的匹配与强度

适用字段：`career_directions`（9 个大类）、`career_specializations`（24 个细分）、`business_directions`（25 个业务方向）。这三个字段在索引里是精确 `array_has_any` 匹配，因此**文本到枚举的映射质量直接决定召回对错**。

**匹配路径：LLM 选码 + 分类器词表反查，交叉验证**

- **A（LLM 选码）**：把三套枚举清单（code + 中文标签，共 58 项）连同判定口径放进解析 prompt，要求模型直接输出枚举 code 或中文标签，而不是自由文本。模型对「AI 应用开发 → BACKEND_AI_APPLICATION」这类近义表达判断准确，但会产生不存在的 code。
- **C（词表反查）**：把同一段查询文本再走一遍 `direction/classifier.py` 的 `_TERMS` / `_SPEC_TERMS` / `_BUSINESS_TERMS`（与简历侧判定**同源**），得到候选 code 集合。该路径确定性、可解释，但对词表未收录的写法无能为力（例如 `_SPEC_TERMS["BACKEND_AI_APPLICATION"]` 现有 "agent / rag / 大模型应用 / llm 应用" 等，缺 "ai 应用"），需补齐常见同义词形。
- **交叉验证规则**：
  - A ∩ C 非空 → 采用（LLM 与词表互相印证）；
  - A 有而 C 无 → 仅当输出与枚举标签**精确命中**时才采用（拦住幻觉 code）；
  - C 有而 A 无 → 不采用（保持"解析器是唯一出口"，避免绕过 LLM 直接下推）；
  - 两者都无 → **不产出该字段**。
- **为什么不做编辑距离模糊匹配**：24 个细分语义密集相邻（如「AI 应用集成」与「全栈交付」），字符串相似度与语义相似度不一致，阈值无法定档，误配代价是把整类人过滤掉。近义判断交给 A，确定性交给 C。

**强度：命中即硬筛选**

- 命中枚举的结果直接下推 `array_has_any`，与用户在「精确筛选」面板手选方向的行为完全一致，不引入"建议态/软排序"这种第二套语义。
- 枚举不到就不产出该字段：该词只保留在 `keywords` 与 `semantic_query` 里照常参与关键词与向量召回，不会因为"没映射上"而凭空产生硬条件，也不会被丢弃。

**已知代价与兜底**：方向标签由 LLM 从简历判定，覆盖率不完整（存量简历、OTHER、未判定者为空），硬筛选会漏掉这些人。兜底沿用已确认的护栏：解析条件在界面上可删可改、`inferred` 条件导致 0 结果时给空结果自诊断、方向枚举误判率纳入验收（必须为 0）。

### 回退链

```text
parse_enabled=false            → 完全走现有链路（parse_query + 可选 rewrite）
parse_enabled=true, LLM 正常   → 逐字段校验后采用；失败字段回退规则值
parse_enabled=true, LLM 失败   → parse_query 产出 filters+keywords
                               + rewrite_enabled 时由 SemanticQueryRewriter 产出 semantic
两者都失败                     → 原词直查（保留现有 degraded 语义）
```

## 关键词 / 精确筛选 / 向量三侧

### 关键词（FTS）

- **词表扩容**：`search/lexicon.py` 增加岗位族（前端/后端/算法/测试/运维/数据等）、行业词、常用缩写（数仓→数据仓库、K8s→Kubernetes 已有、Golang→Go 已有）与同义词组，沿用 `expand_document_tokens` 的「文档侧展开 + 查询侧展开」对称机制，FTS 侧用 OR-of-aliases。
- **重建代价（2026-09-21 实测更新）**：`keyword_index_text` / `body_index_text` 是写索引时生成的，词表扩容后旧索引行不含新别名，必须重投影或全量重建。
  实测规模：候选人 **1,510 个实体 / 30,758 chunk**，每人中位 **18 个 chunk、3,029 字符**（p90 5,683、最大 20,081），全量合计 **524 万字符 ≈ 330 万 token**。
  投影链路已具备并发与配额保护：`search/sync.py` 的 `DEFAULT_SYNC_CONCURRENCY = 4` 与 `EMBEDDING_TOKENS_PER_MINUTE = 400_000`（按上游 **TPM 500,000 的 80%** 设定，留 20% 给交互式检索；**RPM 2,000 不是约束**——TPM 打满时也只有约 3 请求/秒）。据此**全量重建估算约 13 分钟**（纯 token 预算下限 7 分钟），不再是小时级。
  **2026-09-21 重建实测（已完成）**：1,542 个实体（候选人 1,511 + JD 31）在 61 轮 `run_once(batch_size=25)` 内排空，**806 秒 ≈ 13.4 分钟，0 失败**，与估算一致；`INDEX_CHUNK_VERSION` 9→10 走 `plan_index_upgrade` 判定的**就地修复**（补列 + 刷新 metadata，不归档、不换向量口径，旧行全程可读可搜）。
  过程中发现并修复两个真问题（否则这次重建投不进去）：
  1. **发布锁竞争**：`_publish` 用 `BEGIN IMMEDIATE` 独占 SQLite 写锁，而事务里还夹着一次 LanceDB 提交，锁持有时间远大于 `busy_timeout=5000`；并发 4 个 worker 同时进入必然互相超时，表现为 outbox 长期 `RETRY_WAIT` + `last_error=OperationalError`（存量 1,500+ 实体一直投不进去的真因）。修法：**发布串行（asyncio 锁）、快照与 embedding 保持并发**，并加回归测试 `test_sync_publishes_serially_even_with_parallel_workers`。
  2. **`location_terms` 与 `location` 列不同源**：前者按各修订自己的 `parsed_data["location"]` 生成，后者用多修订统一值，导致「显示苏州、按无锡筛」（实测 6/1,477 行）。修法：`_snapshot` 里用统一值生成 `location_terms`，并加回归测试锁定不变量。
  重建后校验：`outbox_pending=0`、`failed=0`、metadata chunk=10；140 行需归一化的 `location` **全部**已在 `location_terms` 里带上规范城市名（`stale=0`）；文档侧别名展开落地（同现行数：数仓+数据仓库 89、前端+前端开发 41、电商+电子商务 48）。脚本与报告：`scripts/rebuild_index_2026_09_21.py`、`.tmp-plan/rebuild-2026-09-21.json`。
  本轮决定：与其它需要重建的改动**合批**，升 `INDEX_CHUNK_VERSION` 触发显式重建，旧索引全程只读可回退。
- **城市词典扩容（含索引侧归一化）**：查询侧扩到全国地级市 + 直辖市 + 「北上广深」类缩写，并保留 `query.py:_SCHOOL_SUFFIXES` 的学校实体保护（「上海交通大学」不得被判为地点）。
  索引侧 `location_terms` 存的是解析结果里 `location` 的**原值**（`search/documents.py` 生成），下游是精确 `array_has_any`，因此**词典产出的字符串必须与索引里的值逐字一致**。
  实测现网写法：1,475 条 `location` 共 172 种，其中 98% 已是规范城市名（上海 516、北京 186、广州 118、深圳 117、西安 74、杭州 60…），但有 **32 条是非规范写法**：`上海市`、`广东省深圳市`、`四川省成都市`、`江苏省无锡市`、`陕西省西安市`、`上海-浦东新区`、`成都/杭州`。这类行用 `array_has_any(["深圳"])` 永远召不回。
  结论：**只改查询词典不够**，必须在 `documents.py` 生成 `location_terms` 时做一次城市归一化（`广东省深圳市` → `深圳`）。这属于索引列内容变更 → 与词表扩容**合批重建**；归一化口径必须与查询词典同源（同一份城市表 + 同一套「去省级前缀 / 去『市』后缀」规则）。
- **多值公司**：`CandidateFiltersRequest` 补 `companies` 字段（`lancedb_index._where` 的 OR 子句已就绪，目前仅 JD 下推使用）。
- 关键词模式仍不接受 `rewrite_enabled`：关键词模式没有向量通道、也不参与重排，语义查询没有可消费的地方，允许它只会让用户误以为"AI 参与了排序"（前端 `client.ts` 已同步兜住以免 422）。**这条限制只针对语义改写，不针对 LLM 解析**：开启解析时，keyword 模式照常取用 filters + keywords。

### 精确筛选

- 解析结果只作为**草稿建议**：类型与面板字段一一对应，面板显式值始终优先（保持 `_merge_filters` 现有语义，不新增第二套优先级规则）。
- 前端把 `parsed_conditions` 映射回筛选面板草稿，允许逐条删除/修改后再搜索。

### 向量

- `semantic_query` 用于查询向量与重排；原查询向量 A 始终存在，改写/解析产物只作补充（沿用现有双查询与 vector family 取最大值的融合口径）。
- 解析不改变索引文本，不需要为「解析」单独重建索引。

## 回显与交互

### 展示「当前生效条件」必须用合并后的最终集合

现状缺口：后端 `_to_response_plan` 回显的是 `parsed.conditions`，即**文本解析**的结果；而真正生效的是 `_merge_filters` 合并后的集合（面板显式值优先、可覆盖解析值）。前端因此只显示解析出的条件，看不到手填面板的条件，用户无法确认"现在到底生效了哪些条件"。本设计必须一并修正。

- `QueryPlanResponse` 扩展：
  - 新增 `effective_conditions`：`_merge_filters` **合并后**的最终生效条件，每项带 `source`（rule / llm / panel）、`confidence`、`span`；
  - 保留 `parsed_conditions`（仅解析侧，兼容旧调用方；前端不再以它作为展示来源）；
  - 新增 `parsed_plan_source`（llm / rule / mixed）、`keyword_terms`、`unparsed_terms`。
- 前端展示四块，缺一不可：
  1. **最终生效条件**：来自 `effective_conditions`，与面板手选条件并列显示，并按来源标注（规则解析 / AI 解析 / 面板手填）；
  2. **可纠正**：chips 可删可改，删除即从生效集合移除，修改即回填筛选面板草稿（面板优先语义不变）；`CONDITION_LABELS` 补齐新字段中文名；
  3. **检索词条**：`keyword_terms` 预览，让用户看到「后端开发 → 后端/服务端」这类别名整合；
  4. **语义查询与未识别片段**：`semantic_query` 与 `unparsed_terms`（既没进条件也没进词条的残句），用于判断"AI 有没有读懂"。
- 其余交互不变：新增独立「AI 智能解析」复选框（默认关闭），与现有「AI 语义改写」并列；混合/向量模式同样渲染「检索经历正文」开关，文案注明「仅影响关键词通道，不影响向量」；三模式仍由用户手选，解析产物按模式取用——keyword → filters + keywords，vector → filters + semantic，hybrid → 三者全用。

### 高风险字段（姓名 / 手机号 / 性别）的护栏

本设计采用 LLM 自由解析，因此必须同时具备：

1. **原文 span 硬校验**：输出值必须能在原文中定位；手机号必须是原文中的完整 11 位号码并通过 `duplicates/service.py:normalize_phone`。
2. **回显可删可改**：chips 与面板草稿均可编辑，面板显式值优先。
3. **空结果自诊断**：因 `inferred` 来源条件导致 0 结果时，前端提示「可能是解析误判」并提供一键去掉推断条件重搜；后端保留原始 plan 便于排障。
4. **误判率纳入验收**：抽检集上姓名/性别误判必须为 0，否则该字段降级为「仅显式语法」。

## 混合正文检索（H2' / H3'）

1. **前端解禁**：`desktop/src/api/client.ts` 去掉 `effectiveSearchBody = mode === "keyword" ? searchBody : false`；`TalentPoolPage.tsx` 在向量/混合模式也渲染开关，沿用 `localStorage` 键 `search-body:v1`。
2. **后端注释与契约**：更新 `api/search.py` 中「仅 mode=keyword 生效」的过时注释；`search_body` 在 keyword 与 hybrid 的含义必须一致（已有 `tests/search/test_service.py::test_search_body_is_forwarded_identically_in_both_modes`，需补 API 级 hybrid 用例）。
3. **混合 FTS 弱命中过滤（H3'）**：
   - 在 `service.py:_parallel_retrieve` 的 FTS 分支接上 `_filter_by_hit_terms`；
   - 并在**候选人级**聚合已策展 concept 命中（复用 `lancedb_index.search_fts_boolean` 的 `_matched_concept_indices` 思路），要求命中 ≥1 个已策展 concept；查询没有已策展 concept 时自动退化为原行为，避免清空召回；
   - 过滤前后计数进 `diagnostics`，便于观测。
4. **验收**：用现有 bench 对 `hybrid × {body 开/关} × {弱过滤 开/关}` 做 A/B；门槛 nDCG@10 降幅 ≤0.01、R@20 不降、空结果率不升、p95 延迟增幅 <20%。
   **已执行并定档（2026-09-21）**：见「阶段 1 A/B 定档结果」与「分层集扩样（20 → 36 JD）与一处真实缺陷」两节。
   结论：**弱过滤保持默认开启**、**正文开关保留解禁**；过程中修掉一个真实缺陷
   （「查询的已策展概念全池无人命中时闸门清空召回」→ 改为失败开放），修复后两套集合的默认形态四项门槛全过。

## 分阶段改动

### 阶段 1：混合正文检索（独立可发布，最小风险）

前端解禁 → 混合 FTS 弱命中过滤 → 注释与契约测试 → bench A/B 定档。不触碰解析与词表。

### 阶段 2：解析基础设施（默认关闭，只回显）

`TaskKind.QUERY_PARSE` + schema + 逐字段校验器 + 缓存 + 回退链 + `parse_enabled` 字段 + `effective_conditions` 等回显字段 + 前端开关与「合并后生效条件」展示。本阶段不改变召回结果，仅当用户主动开启解析时才生效；但"展示合并后生效条件"这一项对未开启解析的用户也立即生效（修正现状缺口）。

### 阶段 3：字段分批启用

1. 年限 / 学历 / 城市 / 年龄 / 性别 / QS / 学校等级；
2. 公司（含多值）/ 职位 / 学校实体；
3. 方向三类（职业大类、职业细分、业务方向，走 A+C 交叉验证，命中即硬筛选）+ 排除项。

每批附单元测试与真实查询抽检（误判率、条件违反数）。

### 阶段 4：词表与城市词典扩容 + 合批重建

lexicon 扩容、城市词典扩容、`location_terms` 城市归一化三者一起做（都是索引文本变更），升 `INDEX_CHUNK_VERSION`，按现有 rebuild maintenance 流程重建，保留旧索引一个观察周期。

**执行前置（2026-09-21 状态）**：索引当前为 schema 10 / chunk 9；画像已全量重算（JD 31 条、候选人 1,716 条），outbox 里已有 **1,539 条待投影**（候选人 1,511 + JD 28）——也就是说无论做不做词表扩容，本来就要重嵌一次。两者**天然合批**：一次重建同时覆盖「画像更新」与「词表/归一化变更」，不要拆成两次。

**执行确认**：重建会长时间占用 embedding 配额（估算约 13 分钟，见「关键词（FTS）」的重建代价），执行前必须先提示使用者并由使用者决定。

**实施状态（2026-09-21）**：阶段 1~4 的代码已落地（阶段 3 的字段启用随阶段 2 一次性实现），「高风险字段护栏」的空结果自诊断（前端提示「可能是解析误判 + 一键去掉推断条件重搜」，逻辑为对推断字段下发显式空值）与全部单元/契约测试也已补齐。
阶段 4 的三项代码就已位，但**故意未升 `INDEX_CHUNK_VERSION`（仍为 9）、未执行重建** —— 升版本后 `sync.py` 会直接抛 `Index version requires rebuild`，在重建完成前索引不可写。因此「升版本 + 重建」作为一个原子动作，等使用者确认后执行。
注意：别名展开（查询侧 OR-of-aliases）与城市归一化的**查询侧**部分即时生效，不依赖重建；词表扩容的完整收益（文档侧 token 展开）与 `location_terms` 归一化需要重建才生效。
另外，`search/documents.py` 的 `location_terms` 已归一化，但 `keyword_index_text` 仍按原文分词（本轮按文档范围未改动）；若后续希望「搜深圳」在 FTS 通道也能召回写成「广东省深圳市」的行，需另开一处改动并一并重建。

**尚未完成（除重建外）**：
1. 阶段 3 的真实查询抽检——**已执行，见下节**；
2. 阶段 5「是否默认开启解析」定档——**已由抽检结果判定：维持默认关闭**（见下节）；
3. 风险表要求的「词表扩容后对匹配与复核分布做回归观测」——**已执行**：冻结配对 3/3 的 `eligibility` 与 2026-09-17 验收基线逐条一致，`tests/match` + `tests/direction` + `tests/search/test_query_parse.py` 共 244 项全绿；词表扩容落点是 `_CURATED_CONCEPTS`（只被 `search/rewrite.py`、`search/service.py`、`search/lancedb_index.py` 消费）与 jieba 自定义词，不进入 `direction/policy.py`，故对匹配分层主因子（职业方向）无影响。
   **pair3 异常已查清（2026-09-21 只读核查，判定为规格冲突而非本次回归）**：`9eeb2bf3737c↔543afa81` 会落到 `match_tier=recommend`，与计划文档「只能待核、不能直接推荐」冲突。三条事实：
   ① 该 JD **没有 `must_skill_groups`**（冻结快照 33 个 JD 修订里 **0 个**带该结构），代码走的是旧分支，**「指定框架」从未成为硬组**；
   ② `missing=()` **不真实**——旧分支只在 `required_skills` **全部**未命中时才记缺口，一旦命中 ≥1 条就留空；按真实缺口算，19 条 required 里 12 条未命中（`asyncio`/`CrewAI`/`Vertex AI SDK`/`GenAI SDK`/`Gemini`…），而那条 Gemini 职责证据是靠 `多/模态/缓存/模型` 这类泛 token 过线的（这正是计划文档说的「因术语接近直接推荐」）；
   ③ 分层公式只要求「方向同向 + 任一技能/职责证据」，公式里**没有**任何表达「JD 明确指出但候选人缺失」的输入，因此必然 recommend。
   最小改动建议（**已按决策执行低风险那步**）：让 `missing_skills` 诚实——`match/policy.py` 旧分支现在把未命中的 `required_skills` 计入 `missing_skills`（不再只在「全部未命中」时才记）。验证：`verify_frozen_pairs.py` 的 pair3 由 `missing=()` 变为 `missing=('asyncio', 'CrewAI')`，`eligibility` 与 `match_tier` 行为不变，`tests/match` 全绿。
   **未做（2026-09-21 决策：暂不做，只登记口径）**：给分层补「JD MUST 缺口 → 待核」降级信号——**不实现**。
   理由：该信号会影响所有配对的 `match_tier`，可能把 pair1「可入选」误降级；且冻结 JD 侧根本没有可用的输入
   （见下条登记），改完实测覆盖面也很窄。两条口径就地登记，供后续接手：
   - **验收口径**：pair3 这类场景的正确断言是「`eligible == True` **且** `match_tier != recommend`」，
     不是「`eligible == True`」——后者在缺降级信号时恒真，等于没验。
   - **冻结集登记**：冻结快照 33 个 JD 修订里 **0 个**带 `must_skill_groups`，因此现有的冻结配对回归
     对「新硬组语义」是**空跑**；要让这类回归真正生效，须先把冻结 JD 按新 schema 重解析（会改动冻结基线，未做）。
4. `scripts/*` / `bench/*` 的解析质量抽检脚本——**已新建**（见下节）；
5. 前端 e2e——**已执行并通过（7/7）**；过程中发现并修掉一个真实 UI 回归：TalentPoolPage 工具栏控件变多后**横向溢出**，相邻按钮被压住导致点击事件被别的元素截走（e2e 里连「关键词」都点不动）。修法与同库的 `MappingPage` 一致：工具栏允许换行（`flexWrap: "wrap"`）；
6. 存量数据：**375 条候选人画像未过机检**（1,539 条 outbox 待投影已随重建清零）。已查清（2026-09-21 只读核查）：
   机检 = `providers/profile_spec.py:vet_profile()`，是**生成期护栏**（不过就带原因回喂模型重写，最多 2 次），**不拦落库**；
   候选人侧规则为非空 / 无换行 / **120~160 字** / 禁学历词 / 禁数字 / 末句不落在评价上。
   当前 **1716 条当前版画像**（1,511 人）：通过 1341、未过 375，且每条恰好命中一条规则——
   **超上限 304**（161~282 字，中位超 12 字）、**不足下限 32**（93~119 字）、评价式收尾 38（长度都合规）、禁写数字 1；
   `points` 不在 `narrative` 中 = **0**（逐字同源 100% 成立）。JD 侧 31 条里 1 条未过（评价式收尾）。
   成因：超长 86% 是模型整段写太长，14% 是 `reconcile_pair()` 在「分点与整段不同源」时按分点重拼放大的。
   影响：**对检索与展示都无功能性破坏**——`_profile_text()` 不做长度截断，282 字远未触顶 BGE-M3 窗口，
   表格单元格只显示 10 字、悬停看全文，所以是「口径失守 + 技术债」，不是坏功能。
   **处置（2026-09-21 决策：压到限内 + 重嵌；已执行）**：分两步、反复迭代到收敛——
   ① 确定性压缩 `scripts/compress_oversized_profiles_2026_09_21.py`：以「分点即唯一事实源」取「最长的过机检前缀」
   （必要时截断最后一点、优先收在句读边界），用 `vet_profile` 本身当判据 → 逐字同源天然成立；
   ② 模型重生成 `scripts/regenerate_noncompliant_profiles_2026_09_21.py`：复用生产回填链路
   （`BackfillService._backfill_one` + 生成器内置的 vet 回喂重写），只对确定性手段做不到的行生效（`force=True`）。
   两轮「重生成 → 压缩 → 重嵌」后：**机检通过 1341 → 1710 / 1716（99.65%）**，未过 **375 → 6**，
   且剩下 6 条全是「不足下限（<120 字）」——属**信息量本身不足**，模型在不编造事实的前提下补不够，
   **2026-09-21 决策：保持现状**（下限规则对这类简历本就不适用；这 6 条对检索与展示均无功能性破坏）。
   复核命令：`py -3.12 scripts/compress_oversized_profiles_2026_09_21.py --data-root .dev-data --verify`
   （输出「当前版画像=1716 分点非逐字同源=0 有正文无分点=0」）。
   契约校验：当前版画像 1716 条、**分点非逐字同源 = 0**、有正文无分点 = 0；索引逐次重嵌，最终 **待投影 0 / 失败 0**。
   备份：`.tmp-profile-compress-backup/profiles-*.json`（三份，整份 `parsed_data`，可回滚）。

### 阶段 1 A/B 定档结果（2026-09-21 实跑）

工具：`scripts/stage1_body_weak_ab_2026_09_21.py`（新）。四条臂 = `search_body × 弱命中过滤` 的笛卡尔积，
同一查询集、同一 `limit=20`、同一索引快照（chunk 10），只改这两个变量。旋钮是新加的模块级常量
`search/service.py:HYBRID_WEAK_HIT_FILTER`（**默认 True = 现网行为**，引入前行为逐字不变），
「旋钮真的改变召回」由单测锁定：`test_hybrid_weak_hit_filter_switch_really_changes_recall`。

**取数必须与生产同口径**：`api/search.py` 传给检索的是规则解析产出的 `keywords + concepts + filters`，
不是原始输入串。第一版取数只传原串，导致概念闸门拿到空 `concepts`（实测 `queries_with_concept_gate=0`），
「弱过滤臂」退化成空壳——该版结果作废并已重跑。下表的弱过滤臂确认生效（见「机制证据」）。

**A. 调参集**（`.tmp-judge/queries.json`，20 JD × 3 问法 = 60 条；JD 级 = 同一 JD 三种问法取均值 → 20 个独立单元）：

| 臂 | 正文 | 弱过滤 | nDCG@10 | R@20 | 空结果率 | p95 延迟 | 门槛 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `body0_weak0`（阶段 1 之前） | 关 | 关 | 0.3974 | 0.3599 | 0.00% | 3875ms | 基线 |
| `body0_weak1` | 关 | 开 | 0.3911 | 0.3634 | 1.67% | 4546ms | ❌ 空结果率 0→1.67% |
| `body1_weak0` | 开 | 关 | 0.3733 | 0.3390 | 0.00% | 6281ms | ❌ nDCG −0.0241、R@20 −0.0209 |
| `body1_weak1` | 开 | 开 | 0.3606 | 0.3308 | 1.67% | 5703ms | ❌ 四项全挂 |

JD 级配对（vs 基线，20 个单元）：`body0_weak1` −0.0063（7 胜 / 10 负 / 3 平）、
`body1_weak0` −0.0241（8/12/0）、`body1_weak1` −0.0368（**5/15/0**）。

**B. 独立验收集**（`.tmp-real-eval/real_queries.json`，99 条真实 match 查询 + 判决式标签）：

| 臂 | 正文 | 弱过滤 | nDCG@10 | R@20 | 空结果率 | p95 延迟 | 门槛 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `body0_weak0` | 关 | 关 | 0.4001 | 0.0974 | 0.0% | 11781ms | 基线 |
| `body0_weak1` | 关 | 开 | 0.4027 | 0.1001 | 0.0% | 10313ms | ✅ 四项全过 |
| `body1_weak0` | 开 | 关 | 0.4115 | 0.0957 | 0.0% | 10515ms | ❌ R@20 −0.0017 |
| `body1_weak1` | 开 | 开 | **0.4242** | 0.0994 | 0.0% | 11203ms | ✅ 四项全过 |

配对（vs 基线，99 个单元）：`body0_weak1` +0.0027（30 胜 / 21 负 / 48 平）、
`body1_weak0` +0.0114（49/48/2）、`body1_weak1` +0.0241（50/47/2 —— 均值靠少数大幅提升拉动，胜负几乎持平）。

**机制证据（弱过滤确实生效，不是空壳）**：概念闸门在 **38/60** 条查询上启用；
FTS 行累计 7097 → 5708（正文关）/ 7169 → 5405（正文开）。
`rows_after_hit_terms == rows_before` —— 长查询下「命中词条」这道闸门不裁剪任何行，
真正起作用的是**候选人级概念闸门**（这也解释了为什么文首两版结果几乎一样）。

**延迟数字不可比**：四条臂串行执行且 reranker 持续返回 `E_API_RATE_LIMIT`（越靠后的臂重试越多），
绝对 p95 被重试污染。重试不改变排序，故 nDCG / R@20 / 空结果率的比较不受影响。

**两套集合结论冲突，不能单方面采信**：
- 调参集：三条改动臂**全部不达标**，其中「正文开 + 弱过滤」在 20 个 JD 里有 15 个变差；
- 独立验收集：两条上线形态（`body0_weak1`、`body1_weak1`）**四项门槛全过**，正文开着反而 nDCG +0.0241。

冲突的成因与取舍依据：
1. 两套标签来源不同——调参集是人工/模型判决的 0–3 级相关性（20 个独立单元，方差大）；
   独立验收集是 99 条真实 match 产出的标签（单元多，但标签本身来自生产排序，存在自证倾向）。
2. 调参集只有 20 个独立单元（同一 JD 的三种问法共用同一候选池），**不足以单独定档**；
   文档 §评测与门槛 把「独立验收集」写成了「默认开启解析」的前置条件，阶段 1 未明确要求，
   但 99 条那套的样本量与「真实性」明显更强。
3. 弱过滤单独作用（正文关）在两套上都≈0（−0.0063 / +0.0027）——它的效果只在「正文打开」时才可观测。

**决策（2026-09-21）：采信独立验收集，同时扩样复核分层集。** 于是先把分层集从 20 JD 扩到 36 JD 重跑。

### 分层集扩样（20 → 36 JD）与一处真实缺陷

扩样上限由语料决定：库里 `deleted_at IS NULL` 的 JD 共 **31 个**（原 20 个里有 6 个后来被软删除），
可新增 16 个 → 合计 **36 个 JD / 108 条查询**。工具：`scripts/judge_eval_expand_2026_09_21.py`
（选样 + 作者化 48 条新问法 + 真检索 + 盲测判决输入，产物在 `.tmp-judge-expanded/`，
**不重新盲化 J01–J20**，否则原判决标签全部作废）。判决由 4 个子 Agent 依据 JD 原文独立完成，
产物 `judge_out_new_batch{1..4}.json`；标签逐字符对齐校验通过（原 20 个 JD 的映射/判决条数也全部相等，
无静默丢分）。

诚实说明：36 个 JD 里有 6 个近重复簇（`J01≈J21`、`J26≈J27`、`J31≈J32`、`J34≈J35` 等，文本相似度 ≥0.80），
**去重后有效独立单元 ≈28**，不是 36。

扩样重跑后（修复前）：

| 臂 | 正文 | 弱过滤 | nDCG@10 | R@20 | 空结果率 | 门槛 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 基线 | 关 | 关 | 0.4333 | 0.3188 | 0.00% | — |
| | 关 | 开 | 0.4194 | 0.3206 | **2.78%** | ❌ nDCG −0.0139（超 0.01 限）+ 空结果率 |
| | 开 | 关 | 0.3807 | 0.2894 | 0.00% | ❌ nDCG −0.0526、R@20 −0.0294 |
| | 开 | 开 | 0.3711 | 0.2840 | **2.78%** | ❌ 四项全挂 |

扩样**把「不达标」判得更硬**：弱过滤单臂的 nDCG 从 20-JD 时的 −0.0063（限内）变成 −0.0139（超限）。

**由此挖出一个真实缺陷（已修）**：新增的 3 条空结果全部是 `vague` 问法、且全是 Avaloq 查询
（`J16:vague`/`J29:vague` = `Avaloq 开发`、`J33:vague` = `Avaloq 团队负责人`），
它们**唯一的已策展概念就是 `Avaloq`，而全池无一人具备**（判决方两边独立确认）。
于是概念闸门「要求候选人命中 ≥1 个已策展概念」把 FTS 通道**全部**滤掉，向量通道又被相对阈值滤空，
整页由「非空」变成「空」——正是文档要求「避免清空召回」却没覆盖的情形：
文档只写了「查询**没有**已策展概念时退化为原行为」，没写「**有** concept 但全池无人命中」。

修法（`search/service.py:_filter_hybrid_fts`）：**失败开放**——闸门会把通道清空时退回原集合，
诊断里加 `hybrid_fts_gate_emptied` 标记。与 `_apply_rerank_min_score` /
`_apply_vector_absolute_fallback` 是同一纪律：质量闸门用于压尾部噪声，不能把整页清空。
回归测试：`test_hybrid_fts_concept_gate_fails_open_when_it_would_empty_channel`、
`test_hybrid_fts_concept_gate_still_filters_when_someone_matches`。

**修复后最终定档（两套集合，chunk 10）**：

| 集合 | `body0_weak1`（默认形态：正文关 + 弱过滤） | `body1_weak1`（正文开 + 弱过滤） |
| --- | --- | --- |
| 分层集 36 JD / 108 条 | ✅ 四项全过（nDCG −0.0076、R@20 +0.0005、空结果率 0%、p95 ×1.04） | ❌ nDCG −0.0635、R@20 −0.0351、p95 ×1.92 |
| 独立验收集 99 条 | ✅ 四项全过（nDCG +0.0031、R@20 +0.0043、空结果率 0%） | ✅ 四项全过（nDCG **+0.0259**、R@20 +0.0036、空结果率 0%） |

**最终决策（2026-09-21，按「检索最优」口径）**：
1. `HYBRID_WEAK_HIT_FILTER` **保持默认开启**——修复前两套集合的默认形态都挂（空结果率 2.78%），
   修复后**两套集合的默认形态四项门槛全过**，这是唯一在两侧都成立的口径。
2. 混合模式的「检索经历正文」开关**保留解禁**：独立验收集（真实查询分布 + 99 条）上
   「正文开 + 弱过滤」是所有臂里最好的一档（nDCG +0.0259）；分层集结论相反，属**已知分歧**，
   已在此记录，后续如需改口径以「独立验收集」为准并重跑。
3. 延迟数字在 A/B 中不可比（串行四臂 + reranker 持续 `E_API_RATE_LIMIT` 重试污染绝对 p95）；
   重试不改变排序，故 nDCG / R@20 / 空结果率的比较不受影响。

### 阶段 3 抽检结果（2026-09-21 实跑）

工具：`scripts/parse_quality_2026_09_21.py`（驱真解析 + 真检索）与 `backend/src/kerui_recruit/bench/parse_quality.py`
（纯函数校验器 + 单测）。校验口径严格对齐生产：原文依据按「忽略空白可定位」、文本字段按 `LIKE` 子串、
学校等级与学历按包含式层级、多修订候选人按「任一行满足即算满足」。
报告：`.tmp-plan/parse-quality-real.json`（99 条带标签真实查询）、`.tmp-plan/parse-quality-judge.json`（60 条分层问法）。

门槛判定（99 条真实查询，复核 3,662 个候选人）：

| 门槛 | 结果 |
| --- | --- |
| 硬条件违反数 = 0 | ✅ 0 |
| 姓名/性别/手机号误判 = 0 | ✅ 0 |
| 方向枚举误判 = 0 | ✅ 0 |
| 城市误判 = 0 | ✅ 0 |
| 解析失败可完整回退 | ✅ 回退率 5.05%（5/99，全部为 timeout，均走规则链路，`degraded_reasons` 语义不变） |
| nDCG@10 不降 | ✅ 0.4406 vs 0.4196（开解析更好） |
| **R@20 有提升** | ❌ **0.0522 vs 0.0641（下降）** |

**阶段 5 结论：未达门槛 → 解析维持默认关闭。** 字段使用画像：方向类条件出现最多（`career_directions` 77/99、
`career_specializations` 76/99，全部为 `inferred`），其次 `highest_degree` 24、`min_years` 20、`title` 10。

**抽检暴露的两个真问题（已修）与一个待决策项**：

1. **方向枚举校验形同虚设（已修）**：`parse.py:_validated_directions` 的「仅认精确标签」比的是 **LLM 自己的输出**
   （提示词要求模型回 code，`raw` 天然等于 code），等于没有原文依据校验——实测「AI 效能 全栈」被解析出
   `OPS`/`ALGORITHM` 等一堆方向硬条件。已改为「C 侧词表反查命中 **或** 原文出现该 code 的精确标签」，
   并加回归测试 `test_parser_drops_direction_code_without_text_evidence`。
2. **`title` 硬条件是空结果的头号原因（已按决策改为软排序）**：11 条空结果里 **8 条由 `title` 造成**
   （该条件单条选择性 = 0 人）。
   根因：索引侧 `title` 就是简历抽出的**过往/现任职位名**（`title_terms`），匹配口径是**整串子串**；
   而 LLM 从 JD 抄出来的是**描述性长短语**，两者整串几乎不重合。诊断见
   `scripts/diag_parse_title_2026_09_21.py`：

   | 查询 | LLM 产出的 title | 整串命中 | 拆成片段后各自命中 |
   | --- | --- | --- | --- |
   | R004 | `Avaloq Squad Lead AVP` | **0** | Lead=110、AVP=1 |
   | R017 | `经验丰富的软件工程团队负责人（VP）` | **0** | VP=19、整串短语=0 |
   | R019 | `自动化测试/QA工程师` | **0** | 自动化测试=4、QA工程师=1 |
   | R043 | `Avaloq Squad Lead` | **0** | Lead=110 |
   | R094 | `全栈工程负责人` | **0** | 全栈工程负责人=0 |
   | R045 | `采购经理` | 3 | 采购经理=3（最终仍 0，说明还有召回窗口因素） |

   **处置（2026-09-21 决策：改软排序）**：`parse.py` 新增 `_SOFT_FIELDS = ("title",)`——职位名不再下推硬筛，
   改为把「原文可定位的职位短语」并入词条，交给 FTS / 向量 / 重排参与打分（`_soft_field_values()`）。
   面板手填的「职位」硬筛语义不变。定点复跑同 11 条查询：**空结果 11 → 2 条**，违规 0、硬条件违反 0。
   回归测试：`test_parser_keeps_title_as_ranking_signal_not_hard_filter`。
3. **公司名与职位名同源，已一并降为排序信号**：`_SOFT_FIELDS = ("title", "company", "companies")`。
   面板手填的「职位 / 公司」仍是硬筛；JD 硬条件下推的 `companies` 也不走解析这条路径。
   回归测试：`test_parser_keeps_company_as_ranking_signal_too`（多值公司逐个并入词条）。
4. 说明：`condition_selectivity` 给出的是**上界**（忽略召回窗口），所以「上界=0」是结构性无解的确证，
   而上界>0 只说明不是条件卡死（R096 上界 49 但结果 0，属召回/排序窗口问题）。
5. **分层问法集的 6 条违规已逐条复跑定性（2026-09-21），结论：全部为陈旧结果，非真违规。**
   旧报告 `.tmp-plan/parse-quality-judge.json`（19:57 跑）里 `hard_filter=2` + `span_missing=4`，
   分布在 `J05:vague / J08:colloquial / J10:standard / J11:vague / J14:colloquial / J20:colloquial` 各 1 条，
   **6 条全部只涉及 `title`**：2 条是 title 被下推成精确筛选后「无职位命中」，
   4 条是 span 校验**空白敏感**造成的假阳性（`数仓技术TL` 原文写作 `数仓技术 TL`）。
   定点复跑同一 6 条（`--only` 精确到 `qid:phrasing`，报告 `.tmp-plan/parse-quality-judge-rerun.json`）：
   **违规 0、硬条件违反 0（复核 277 人）、方向误判 0、城市误判 0**，
   且这 6 条的 `title` 一律不再进 `accepted_fields` —— 它已按上文第 2、3 条并入词条参与打分，
   两类违规**在构造上已不可能发生**；`J11:vague` 因此从「只有 title 一个条件」变为零条件（纯 FTS/向量召回）。
   即：第 2、3 条改动同时消解了这 6 条；`_has_span` 的 `_flat()`（去空白 + casefold）消解了其中 4 条。
   另记一条口径观察：`J11:vague` 零条件后检索 `on=50 / off=50` 且无空结果，说明「丢掉 title 硬条件」不会把结果放空，
   这正是软排序决策的预期形态。

### 阶段 5：默认值定档

是否默认开启解析，需独立验收集（60–100 条分层查询）达标后再定；未达标保持默认关闭。

## 文件影响范围

| 文件 | 责任 |
| --- | --- |
| `backend/src/kerui_recruit/search/query.py` | 保留为规则兜底；抽出可复用的字段级解析器供校验器调用 |
| `backend/src/kerui_recruit/search/parse.py`（新增） | LLM 解析 prompt、schema、逐字段校验与回退、缓存 |
| `backend/src/kerui_recruit/search/contracts.py` | `QueryPlan` 增 `effective_conditions`（合并后生效条件）、`parsed_plan_source` / `keyword_terms` / `unparsed_terms`；`ParsedCondition` 增 `source` / `span` |
| `backend/src/kerui_recruit/search/rewrite.py` | 校验器与缓存被解析复用；本身语义不变 |
| `backend/src/kerui_recruit/search/lexicon.py` | 岗位族/行业/缩写扩容（文档侧与查询侧对称） |
| `backend/src/kerui_recruit/search/service.py` | 解析接入与回退链；`_parallel_retrieve` FTS 分支接弱命中过滤 |
| `backend/src/kerui_recruit/search/documents.py` | 词表扩容后文档侧 token 展开口径确认（不改结构）；新增 `location_terms` 城市归一化（索引列内容变更，触发重建） |
| `backend/src/kerui_recruit/direction/policy.py` | 新增中文标签 → code 反查助手；输出 9 / 24 / 25 三套枚举清单，供解析 prompt 与白名单校验共用 |
| `backend/src/kerui_recruit/direction/classifier.py` | 复用 `_TERMS` / `_SPEC_TERMS` / `_BUSINESS_TERMS` 做方向枚举反查（与简历侧判定同源），并补齐常见同义词形 |
| `backend/src/kerui_recruit/providers/ai/contracts.py` | 新增 `TaskKind.QUERY_PARSE` |
| `backend/src/kerui_recruit/runtime.py` | 装配解析器客户端（复用 task_client 模式） |
| `backend/src/kerui_recruit/api/search.py` | 新增 `parse_enabled`；`CandidateFiltersRequest` 补 `companies`；修正 `search_body` 注释；回显 `effective_conditions` 与解析新字段 |
| `desktop/src/api/client.ts` | 解禁混合正文开关；透传 `parse_enabled` |
| `desktop/src/pages/TalentPoolPage.tsx` | 新增 AI 解析开关；正文开关在混合/向量模式可见；展示合并后的最终生效条件（可删可改）；词条/语义/未识别回显 |
| `desktop/src/App.tsx` | 新开关状态与持久化；回显状态接线 |
| `backend/tests/search/*`、`backend/tests/api/*` | 解析校验、回退链、`search_body` 模式一致性、弱命中过滤 |
| `desktop/e2e/search-controls.spec.ts` | hybrid + `search_body` 请求体断言；AI 解析开关断言 |
| `scripts/*` / `backend/src/kerui_recruit/bench/*` | hybrid 正文与弱过滤 A/B、解析质量抽检 |

## 全系统接口回归（2026-09-21 实跑）

工具：`scripts/api_inventory_2026_09_21.py`（从 router 注册表导出接口清单）+ 
`scripts/api_functional_test_2026_09_21.py`（真启动 + 逐条探针）。

**真启动**：`runtime.create_runtime_app(sidecar.build_settings(RuntimeArgs(...)))`
——与桌面端 sidecar 同一入口，跑 lifespan（worker / scheduler / index sync 全起），
请求带 `X-Kerui-Session` 走同一套本地会话校验；数据根 `.dev-data`，真实调用已配置的模型 API。

**覆盖度可证**：清单与运行时注册表同源（**161 条业务路由** + 3 条 health），
每条路由都必须有探针，漏掉的计入失败。最终 **未覆盖 = 0**。

**结果：pass 167 / fail 0 / error 0 / 未覆盖 0**（报告 `.tmp-api/api_test_report.json` 与 `.md`）。

写入类接口一律用**一次性夹具**，不触碰库内既有数据：3 个合成简历 docx（导入按内容哈希去重，
所以不能复用同一份文件）+ 3 个夹具岗位 + 1 个夹具公司 + 1 个映射项目 + 1 个提醒，
「建 → 验 → 改 → 验 → 删」走完整循环；软删除只在夹具岗位上做（候选人侧只支持物理删除）；
不可逆动作只探守门路径并在报告里标 `validation_only`：快照恢复（不存在的文件）、
数据迁移（目标非空目录）、批量合并（单 id 不足以合并）、回填（非法 kind）、空导入。

**过程中修掉的 3 个真实接口缺陷**（都有回归测试）：

| 缺陷 | 原行为 | 修法 | 回归测试 |
| --- | --- | --- | --- |
| `POST /api/backup/restore/{filename}` 文件不存在 | `500 E_INTERNAL`，并把英文内部消息 `Backup not found: …` 透给前端 | `FileNotFoundError` → `404 E_BACKUP_NOT_FOUND` | `test_missing_snapshot_returns_structured_not_found` |
| `POST /api/resumes/candidate/{id}/regen-profile` 供应商不可用 | `except Exception` 把 `E_AI_NO_PROVIDER` 包成 `500 E_PROFILE_GENERATION_FAILED` | `ProviderError` 放行 → 全局处理器映射 502/503（与平台约定一致，前端才能区分可重试） | `test_profile_regeneration_surfaces_provider_unavailable_as_502` |
| `POST /api/jd/{id}/regen-profile` 同上 | 同上 | 同上 | `test_jd_profile_regeneration_surfaces_provider_unavailable` |

**前端**：`npm test`（vitest）**158 passed / 13 files**；
`npm run test:e2e`（playwright，mock sidecar + `.e2e-data`）**16 passed / 0 failed**。

e2e 首轮 3 条失败，逐条查到根因后**都不是环境噪声，而是三类真实问题**（已全部修掉）：

| 现象 | 根因 | 修法 | 回归保护 |
| --- | --- | --- | --- |
| JD 管理页头部「共 1 个在招岗位」旁边列表仍是「岗位列表 0 个 / 暂无 JD」 | **真 UI bug**：`submitJd` / `uploadJd` 导入成功后只调 `loadJds()`（喂头部计数），**没调 `loadJdsPage()`**，分页列表停在导入前 | 两个导入处理器都改成 `await Promise.all([loadJds(), loadJdsPage(1, jdFilter)])` | `App.test.tsx::refreshes the paged JD list after importing a JD`（已用「临时还原旧行为必失败」验证过有牙齿） |
| 导入简历后搜 `Python` 永远 0 结果 | **fixture 不可解析**：`tests/fixtures/resume.pdf` 只有 21 字符（`Python Finance Resume`），未配置 AI 的 e2e 环境里本地规则解析凑不够完整性门槛（≥3 信号且技能/画像/经历至少两项）→ 修订 `E_PARSE_INCOMPLETE` → 入不了索引 → 三种模式都 `index_not_ready` | 新增带文本层的 `tests/fixtures/resume-e2e.pdf`（342 字符中文简历，PyMuPDF 生成），两个 search 用例改用它；本地规则解析即判定「有效」 | 生成脚本自带「内容必须过 `check_parsed_resume`」的自检 |
| 同上场景下「按 /Python/ 找行」始终找不到 | **选择器过时**：结果表列是 姓名/电话/学校学历/…/操作，**不含技能列**，行名里永远没有 `Python` | 行断言改为按已渲染字段（候选人姓名）定位，并补断言「1 条搜索结果」以保留「按技能词能命中」的意图 | — |
| e2e 的 mock 结构化输出只有 `{"ok": true}` | 任何走 AI 结构化解析的路径（配好 AI 后的简历/岗位解析）都会产出空壳 → 判「不合格」 | mock 改为回一份字段超集 JSON，同时满足候选人 `ParsedResume` 与岗位 `ParsedJd` | `test_validity.py::test_mock_structured_payload_passes_validity_check` |

另外记录一条**不能做**的事：不要为了让 e2e 「有 AI」而在 `build_runtime` 里按 `KERUI_AI_MOCK`
自动写入一份 AI 连接——`tests/ai-settings.spec.ts` 的首启向导用例依赖「一开始没有连接」这个初始态，
自动注入会让它的「添加 AI 服务」入口消失（实测该用例因此从通过变失败，随后回退）。
`runtime._mock_ai_provider_config()` 只作为显式可用的工厂保留。

**后端单元/契约测试**：全量 **1403 passed / 0 failed**（含本轮新增的 3 条接口缺陷回归、
2 条混合过滤失败开放回归、1 条 mock 载荷回归）。

**后端接口功能测试**：**167 探针 / 161 条业务路由全过、fail 0、error 0、未覆盖 0**
（本轮改动只涉及 mock 与前端，接口级结论不变）。

## 测试与验收

### 单元与契约测试

1. `search_body` 在 keyword 与 hybrid 下语义一致；hybrid + `search_body=true` 经 API 入口可召回只出现在经历/项目正文的候选人。
2. 混合 FTS 弱命中过滤：只命中一个泛词的行被过滤；查询无已策展 concept 时不启用过滤且不清空结果；过滤计数进诊断。
3. 解析校验：原文 span 缺失的公司/学校/姓名被丢弃；越界区间被拒；未知枚举被丢弃；`semantic_query` 违规被拒并静默使用原查询。
4. 回退链：LLM 超时/异常 → 规则解析；两者都失败 → 原词直查，`degraded_reasons` 与 `empty_reason` 语义不变。
5. 面板优先：同一字段同时来自解析与面板时，面板值生效。
6. 姓名/手机号/性别：仅原文完整 span 生效；`inferred` 条件导致 0 结果时返回可识别的诊断信息。
7. 词表扩容后：文档侧与查询侧别名展开对称（同一概念的不同写法互相命中）。
8. 关键词模式开启 AI 解析时不产生 `semantic_query` 且不触发 422。
9. 生效条件集合：解析值与面板值冲突时，`effective_conditions` 体现面板值，且每条带 `source`（rule / llm / panel）；前端不再展示"只解析、未生效"的旧条件。
10. 方向枚举：LLM 选码与分类器词表反查一致时才下推硬筛选；「AI 应用开发」这类与枚举标签差字的输入能映射到 `BACKEND_AI_APPLICATION`；映射不到时不产出方向条件、不进硬过滤。
11. 城市归一化：`广东省深圳市` / `上海市` / `成都/杭州` 这类写法归一为规范城市名后写入 `location_terms`，搜「深圳」能召回这些行；归一化表与查询词典同源（同一份城市表、同一套去前缀/去后缀规则）。
12. 重建规模与配额：全量重建（1,510 实体）在并发 4 + 400k TPM 预算下完成，过程中不产生上游限流错误；交互式检索查询不被后台重嵌挤到限流。

### 评测与门槛

- 查询集：60–100 条，按「精确技能/岗位名、缩写与中英文混合、模糊短查询、自然语言职责、行业/业务场景、带硬条件、宽岗位词」分层。
- 指标：R@20/50/100、P@5/10、nDCG@10、首位合格率、无结果正确率、硬条件违反数、p50/p95 延迟、解析字段误判率、回退率。
- 门槛：
  - 阶段 1：nDCG@10 降幅 ≤0.01、R@20 不降、空结果率不升、p95 延迟增幅 <20%；
  - 解析：硬条件违反数为 0、姓名/性别误判为 0、方向枚举误判为 0、解析失败可完整回退；
  - 回显：`effective_conditions` 与后端实际下推的 filters 一致（不得只展示解析侧条件）；
  - 默认开启解析：需在独立验收集上 nDCG@10 不降且 R@20 有提升，否则维持默认关闭。

## 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| LLM 幻觉出原文没有的条件/实体 | 原文 span 硬校验 + 逐字段回退 + 诊断记录 |
| 解析拖高搜索延迟 | 2.5s 子预算 + 缓存 + 默认关闭 + 失败回退规则链路 |
| 姓名/性别/手机号误判直接缩小结果集 | 回显可删可改 + 空结果自诊断 + 误判率验收，必要时降级为仅显式语法 |
| 方向类硬筛选漏掉方向未判定的存量简历 | 命中枚举即硬筛选是本轮决策；靠回显可删可改 + 空结果自诊断 + 方向枚举误判率为 0 兜底；若抽检显示漏召明显，再单独评估改软排序 |
| 词表扩容需一次全量重建 | 与其它重建项合批、升 `INDEX_CHUNK_VERSION`、旧索引只读回退；投影已具备并发与 token 预算保护（约 13 分钟），但执行前仍需使用者确认 |
| 词表扩容间接改变匹配/复核分布 | lexicon 被匹配侧复用（`match/policy.py` 用 `tokenize_lexical_text` 判技能命中与职责证据），扩容会改变匹配分层与复核结论分布；扩容后必须对匹配与复核做一轮回归观测 |
| 城市写法不规范导致漏召（现状已存在） | 索引侧 `location_terms` 归一化 + 查询词典同源；验收断言「`广东省深圳市` 归一为 `深圳`、搜『深圳』能召回」 |
| 混合正文噪声污染融合池 | 弱命中过滤 + A/B 定档，未达标不启用 |
| 解析与面板出现两套优先级 | 明确面板优先，解析仅作草稿建议，不新增第二套规则 |

回滚顺序：先关 `parse_enabled` 与正文开关（前端即生效）→ 再回退弱命中过滤 → 最后回退索引重建。

## 非目标

- 不做自动模式路由（不新增 `mode=auto`），三模式仍由用户手选。
- 不改动 RRF 权重、召回配额与重排窗口等已定档的检索参数。
- 不把 AI 生成的同义词写入 FTS 索引（索引侧只做词表别名展开）。
- 不改动匹配（match）侧的 JD 解析、硬条件下推与复核 prompt；但词表扩容会经分词间接影响匹配评分与复核分布（见风险表），扩容后需做回归观测。
- 不在本次把「画像重嵌」与「词表扩容重建」拆成两次重建：两者合批一次完成。
- 不更换 embedding / reranker 模型，不改变向量文本口径（v9/v8 结论另行处理）。
- 本条设计阶段只落盘文档，不包含任何代码改动。
