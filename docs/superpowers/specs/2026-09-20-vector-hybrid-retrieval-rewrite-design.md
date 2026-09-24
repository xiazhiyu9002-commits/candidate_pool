# 向量检索、混合检索与 AI 语义改写优化设计（2026-09-20）

## 目标与结论

本设计优化人才库的纯向量检索、混合检索和可选 AI 语义改写。目标不是继续凭经验调整相似度阈值，而是修正当前检索数据流中已经确认的证据使用不充分、查询表达混用和评测口径偏差，使召回、融合与重排分别使用适合自己的文本和信号。

核心决策如下：

1. **候选人侧向量文本由纯向量和混合模式共用。** 不为两种模式维护两套索引；差异放在查询构造、分类型召回配额、融合权重和降级策略中。
2. **按用途拆分三种文本面。** FTS 使用词法文本，embedding 使用简洁且可追溯的语义文本，reranker 使用候选人级多片段证据包；不再让一个 `vector_text` 同时承担所有用途。
3. **AI 改写从“联想扩写”改成“保真规范化”。** 原查询永远保留，改写查询只作为补充向量召回，不覆盖原查询；reranker 默认使用原查询，而不是同义词堆叠后的长字符串。
4. **先修确定性链路，再重建向量。** 先解决混合模式正文开关失效、最佳向量片段虽已保留但未进入搜索重排、请求 `limit` 影响排序等读侧问题；之后才进行隔离索引消融和生产切换。
5. **AI 改写继续默认关闭。** 只有在完整生产 API 链路、扩大后的盲测集上达到本文发布门槛，才讨论默认开启。

本设计不改变结构化硬条件的含义，不把城市、学历、年限、学校、姓名、手机号等字段重新塞入向量，也不新增建索引阶段的生成模型调用。

### 基线口径

本文的“当前工作区”指 2026-09-20 复审时的未提交工作树，而不是仅指 Git `HEAD`：

- Git `HEAD`（初始代码基线）尚未包含多片段证据包，前端向量/混合默认返回数为 100；
- 当前工作区已经加入 `EvidenceChunk`、`select_evidence()` 和 `SearchHit.evidence`；
- 2026-09-19 的漏斗实验以当时的 `limit=100` 为“现役”基线，文中采用默认 50 是该实验之后已经落入工作区的产品决策，不能把两者当作同一时间点的现状。
- 2026-09-20 复审后移除了人才库的「返回条数」输入框：它会把纯精确筛选（无查询词）也一并截断到该值。现在前端不再传 `limit`，改由 `client.ts` 按默认决定——有查询词的向量/混合 50，关键词与纯筛选 2000（返回全部匹配）。因此 `limit` 只剩 API 调用方与脚本可改，「用户改条数影响排序」已无 UI 入口，但内部耦合仍在（见 H5）。

下文的问题表以当前工作区为准；历史实验数字只用于解释决策来源。

## 当前事实与问题定位

### 现有数据流

当前搜索链路可以简化为：

```text
用户查询
  → parse_query 提取硬条件并保留关键词
  → keyword：原关键词 → FTS
  → vector：可选 AI 改写 → embedding → vector recall
  → hybrid：原关键词 FTS + 改写后的向量查询 → 候选人级 RRF
  → 召回层附带最多 3 条 EvidenceChunk，但搜索 reranker 仍只读取单条代表 vector_text
  → 改写后的查询 + 单条代表 vector_text → reranker
  → 候选人去重与 limit 截断
```

纯向量和混合模式都调用同一个 `LanceDBSearchIndex.search_vector()`，所以索引中的候选人 `vector_text` 天然是共享资产。当前不需要为模式复制索引。

### 已确认的问题

| 编号 | 问题 | 当前代码事实 | 影响 |
| --- | --- | --- | --- |
| V1 | 搜索 reranker 仍只读取单条代表文本 | `_candidate_rows()` 已收集 grouped rows 并附带 `SearchHit.evidence`，但 `search/service.py` 仍用 `hit.vector_text or hit.content` 构造 reranker 输入 | 多片段文本虽已保留给 match 侧，搜索排序仍看不到触发向量召回的 child。 |
| V2 | 原始 `kind` 与重排槽位混为一谈 | `select_evidence()` 强制不同 kind，`_evidence_pack()` 又把排名第一的任意 kind 写入 `parent` 键；候选人实际有 `parent/profile_point/experience/project` 四种 kind | `profile_point` 没有明确去向；首条为 profile_point/experience/project 时会被误标为 parent，同 kind 的两条查询相关证据也无法同时保留。 |
| H1 | 候选人级融合结果与“代表行字段”耦合 | `_rrf()` 的融合分已按候选人分别取 BM25/vector 最佳 rank 后求和，排序信号没有被 BM25 行覆盖；但随后用 BM25 先遍历的 `rows.setdefault()` 决定 `chunk_id/content/vector_text`，并把该行塞到证据首位 | 真正风险是 reranker 输入、API `content` 和 `reasons` 随代表行策略变化，而不是 RRF 分本身。若简单改成“向量行优先”或跨通道比较原始 rank，会制造新的接口漂移。 |
| H2 | `search_body` 没有进入混合链路 | `search()` 调 `_parallel_retrieve()` 时未传该参数；内部 FTS 使用默认 `False` | UI 的“检索经历/项目正文”开关对混合模式无效。 |
| H3 | 混合 FTS 缺少概念覆盖信号 | `_parallel_retrieve()` 直接调用 `search_fts()`，不走 `_filter_by_hit_terms()`；后者阈值仍为 1 | 只命中一个泛词的结果可能占用融合池。 |
| H4 | 聚合后丢失各通道原始信号 | RRF 使用通道内 rank 本身是有意的 rank fusion；问题是融合后的 `SearchHit.score` 只剩 RRF 分，BM25 rank/score 与各向量查询的 rank/similarity 均丢失 | 无法做可靠的权重校准、阈值兜底或结果解释，也容易诱使后续代码直接比较语义不可比的 raw rank/score。 |
| H5 | 请求 `limit` 会改变内部候选池 | `_pool_limit(limit)` 根据请求返回数计算召回池；前端已于 2026-09-20 移除返回条数入口并不再传 `limit`，但 API 调用方与脚本仍可传 | 同一查询换一个 `limit`（如 20/50/100）时，前 20 的排序也可能变化。UI 已不可触发，评测脚本、深分页与将来的 API 调用方仍受影响。 |
| R1 | AI 改写发生语义漂移 | 当前提示词只限制硬条件，真实输出会新增 Spring、Redis、高并发、支付清算等概念 | 结果变化很大，但不是对原始意图的保真扩展。 |
| R2 | 同一改写串同时服务 embedding 与 reranker | `semantic_query_used` 同时传给查询向量与重排器 | 同义词列表可能扩大召回，却同时稀释重排判断标准。 |
| R3 | 改写 A/B 没走完整生产入口 | 现有脚本直接调用 `service.search(text, CandidateFilters())` | 没有覆盖 API 层硬条件提取与 `retained_keywords` 的真实行为。 |

### 当前工作区已完成与仍缺失

| 已在当前工作区实现 | 本设计仍需完成 |
| --- | --- |
| `EvidenceChunk(kind, text, score)` | 在其上补充多通道 `ChannelSignal` 与证据路径，不另建平行证据类型 |
| `_candidate_rows()` / `_rrf()` 收集 grouped rows 并调用 `select_evidence()` | 将候选人融合信号、reranker 证据与稳定展示投影拆开；不再让通道遍历顺序决定公开字段 |
| `SearchHit.evidence` 最多携带 3 条不同 kind 文本 | 把“不同 kind”改为“不同 chunk/文本”；搜索侧按 overview/primary/complementary 选槽，允许两条同 kind 的有效证据 |
| match 侧 `_evidence_pack()` 已用结构化 JD/候选人字段挑技术/业务片段，但把首条无条件标为 parent | 修正真实 parent/overview 规则并保留 match 专用技术/业务标签；搜索侧不复用该分类规则，只复用 `EvidenceChunk` 与选择基础设施 |
| `POOL_LIMIT_CAP=120`、`RECALL_MIN=100`、`RERANK_DOCS=100` | 解耦请求 `limit` 与内部 pool，并保持上述召回/重排基线 |
| 前端已移除返回条数入口，`limit` 由 client 默认（有查询词的向量/混合 50，关键词与纯筛选 2000） | 用固定内部 pool 验证 `limit=20/50/100` 的公共前缀稳定性 |

实施任务不得重新创建已经存在的 `EvidenceChunk` 或另建平行证据类型；应扩展 `select_evidence()` 并修正 match 证据包的槽位规则，再补齐缺失的检索信号与消费路径。

### 已有量化证据

20 条模糊查询的改写 A/B 中，40/40 次改写成功、无重排降级，但质量收益不稳定：

| 模式 | 指标 | 改写关 | 改写开 | 变化 |
| --- | --- | ---: | ---: | ---: |
| vector | nDCG@10 | 0.8444 | 0.8240 | -0.0204 |
| vector | P@5 | 0.6500 | 0.7600 | +0.1100 |
| hybrid | nDCG@10 | 0.8486 | 0.8740 | +0.0254 |
| hybrid | P@5 | 0.6900 | 0.7100 | +0.0200 |
| vector / hybrid | 首位合格率 | 0.60 / 0.75 | 0.60 / 0.75 | 0 |

从已有结果重新计算，改写开关前后的 top-10 集合平均 Jaccard 只有 vector 0.2311、hybrid 0.2998，20/20 条查询排序均发生变化。结论不是“改写没执行”，而是“改写造成大幅波动，但没有可靠提升”。

现有漏斗实验也不支持盲目扩池：`pool=120 → 400` 的 R@20 只增加约 0.0008；把重排窗口缩到 50 会令重排失去跨池选人能力。该实验的“现役”对照使用 `limit=100`；实验之后当前工作区已把产品默认返回数改为 50，并在 2026-09-20 复审后移除了前端的返回条数入口（`limit` 只剩 client 默认与 API 调用方可改）。因此后续实施保持 `pool=120`、每路召回下限 100、重排 100、默认返回 50，避免同时改变过多变量，同时继续把旧实验结果标注为 `limit=100` 基线。

### 2026-09-20 实测结果（调参集）

以下为实施完成后的实测，查询集为 `.tmp-judge/queries.json` 的 20 个 JD × 3 种问法（vague / colloquial / standard），共 **60 条查询**；标注为 `.tmp-judge` 的既有判决集；指标口径与 `judge_eval_metrics_2026_09_19.py` 完全一致（nDCG@10 分级增益、P@5 强相关占比、hits@10 即已判决强相关集被前 10 命中的比例）。**该集是调参集，不是独立验收集**，因此下列结论只用于否决明显退化的方案，不能用于定档。

读侧档位消融（`--suite readside`，产物 `.tmp-ablation/readside.json`，120 个查询×模式对）：

| 档位 | nDCG@10 | P@5 | hits@10 | 首位合格率 | ΔnDCG（胜/负/平） |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline（等权 RRF，全局召回） | 0.4574 | 0.4617 | 0.2630 | 0.4917 | — |
| quota20 | 0.4542 | 0.4700 | 0.2626 | 0.5417 | −0.0032 (48/48/24) |
| quota40 | 0.4628 | 0.4733 | 0.2716 | 0.5417 | +0.0053 (46/48/26) |
| quota60 | 0.4618 | 0.4767 | 0.2689 | 0.5417 | +0.0043 (48/49/23) |
| fts1.25/vec1.0 | 0.4607 | 0.4683 | 0.2651 | 0.5000 | +0.0033 (30/18/72) |
| fts1.0/vec1.25 | 0.4584 | 0.4633 | 0.2634 | 0.4917 | +0.0010 (28/18/74) |
| coverage_tiebreak | 0.4581 | 0.4700 | 0.2630 | 0.4917 | +0.0006 (20/16/84) |
| coverage_light | 0.4580 | 0.4650 | 0.2627 | 0.4833 | +0.0005 (23/15/82) |

结论：没有任何档位取得可信收益（最大 ΔnDCG 仅 +0.0053，胜负基本持平；逐查询胜负的双侧符号检验 p 全部 ≥ 0.11），所以**不切换权重、不启用分类型配额与概念覆盖率**，保持基线默认。一个需要独立验收集复核的信号：分类型召回把**首位合格率一致抬高 5 个点**（0.4917 → 0.5417）、P@5 也略升，而 nDCG 只微动——即“更容易把强相关放在第一位”，但整体排序质量不变；换算成计数只有 120 个「查询×模式」对里的 6 个翻转（约 3 条查询），幅度同样在噪声量级。上表每行的均值都在 vector 与 hybrid 两个模式上取平均。

需要一并记住评测分辨率：本集 60 条查询，均值指标每变动 1/60 ≈ 0.017 才等于「一条查询翻转」，因此低于该量级的 Δ 只能当噪声，不能当结论。

三模式基线对比（`--modes keyword,vector,hybrid --configs baseline`，产物 `.tmp-ablation/readside-modes.json`，同一 60 条查询、同一现役索引 chunk 9；keyword 按设计不重排）：

| 模式 | nDCG@10 | P@5 | hits@10 | 首位合格率 |
| --- | ---: | ---: | ---: | ---: |
| keyword（纯 FTS） | 0.4242 | 0.3667 | 0.2119 | 0.3333 |
| vector | 0.4452 | 0.4533 | 0.2543 | 0.4500 |
| hybrid | 0.4719 | 0.4733 | 0.2711 | 0.5167 |

结论：hybrid 在全部四项指标上都优于 vector，vector 又全面优于不重排的 keyword——说明「混合召回 + 语义重排」这条主线方向正确，默认走 hybrid 有实测支撑；keyword 的落后里含设计取舍（§目标架构明确 keyword 不参与重排），不是缺陷。该次运行的 p50 / p95 延迟为 2281 / 3703 ms（三模式混采）。

改写 A/B（生产 `/api/search/candidates` 入口，产物 `.tmp-ablation/rewrite-ab.json`）：

| 模式 | 指标 | 改写关 | 改写开 | Δ（胜/负/平） |
| --- | --- | ---: | ---: | --- |
| vector | nDCG@10 | 0.4036 | 0.4173 | +0.0137 (19/17/24) |
| vector | P@5 | 0.3967 | 0.4033 | — |
| hybrid | nDCG@10 | 0.4491 | 0.4174 | **−0.0317 (13/29/18)** |
| hybrid | P@5 | 0.4533 | 0.4033 | — |
| hybrid | 首位合格率 | 0.5333 | 0.4333 | — |

改写状态分布（六值枚举）：`success` 72 / `unchanged` 38 / `rejected` 10 / `unavailable` 0；排序变化率 vector 31/60、hybrid 34/60，top-10 Jaccard 均值 0.8409 / 0.7546。

结论：改写**在 hybrid 上显著退化 3.2 个 nDCG 点且胜负比 13:29**，在 vector 上仅微弱提升 1.4 点且胜负持平。因此连「保持可选、默认关闭」的最低条件都不满足，**必须继续默认关闭**；在返工（例如削减 B 臂权重、只让改写参与召回不参与融合）并用独立验收集重测之前，不应再讨论默认开启。

一条尚未闭环的工程事实：全量重嵌入的**持续吞吐约 7 文本/秒**（sidecar 对 1,542 个实体、约 3 万条文本的重建耗时 71 分钟），突发探针能到 77 文本/秒但不具可持续性；上游会以 `E_API_UPSTREAM` 限流，重试后仍能收敛。任何需要反复全量嵌入的消融都要按此换算机时。

v8/v9 隔离索引消融（`--suite index`，产物 `.tmp-ablation/index-v8v9.json`；两臂同一快照、同一 embedding 模型、各 30,671 chunk）：

| 模式 | 指标 | v8 | v9 | Δ（胜/负/平） |
| --- | --- | ---: | ---: | --- |
| vector | nDCG@10 | 0.4650 | 0.4416 | **−0.0235 (20/37/3)** |
| vector | P@5 | 0.4833 | 0.4467 | — |
| vector | hits@10 | 0.2664 | 0.2499 | −6.2%（相对） |
| hybrid | nDCG@10 | 0.4789 | 0.4728 | −0.0060 (23/23/14) |
| hybrid | P@5 | 0.4833 | 0.4800 | — |
| hybrid | hits@10 | 0.2802 | 0.2751 | −1.8%（相对） |
| 合并 | nDCG@10 | 0.4720 | 0.4572 | −0.0148 (43/60/17) |

归因已单独核实：两臂 `keyword_index_text` 在抽样 150 个候选人上**完全一致**、`vector_text` 150/150 不同，chunk 数一致 → 差异纯出自 v9 的向量文本改动，不混合词法面变化。

对照 §发布门槛「向量文本 v9」：`Recall` 类指标两条都是**下降**（要求是相对 +5%），vector nDCG@10 下降 0.0235（超过「不超过 0.01」），hybrid nDCG@10 下降 0.0060、P@5 下降 0.0033（在 0.01 容差内）。

显著性必须分开说：**只有 vector 臂的退化站得住**（逐查询 20 胜 37 负，双侧符号检验 p=0.033）；hybrid 臂 23 胜 23 负（p=1.000）是中性；两臂合并 43 胜 60 负（p=0.114）**未达显著**。因此准确表述是「v9 显著拖累纯向量召回，对默认的 hybrid 无统计可辨影响」，而不是「v9 整体更差」。按文档规则「未同时满足时继续使用 v8，不因完成重建而切换」，v9 未通过门槛；但该判定用的是调参集，且 hybrid 臂的中性结果意味着切换与否对默认路径影响有限，回退决定应在独立验收集上复核后执行。

### v9 相对 v8 到底改了什么（代码依据：`search/documents.py`）

| 位置 | v8 | v9 |
| --- | --- | --- |
| 父向量 | 画像 + 学历文本 + 城市 + 年限 + 最近公司 + 最近职位 + **全部**技能 | 画像 + 业务方向标签 + 最近职位 + 技能（`PARENT_SKILL_LIMIT=24`） |
| 子片段前缀 | 画像浓缩前缀 60 字 | 画像浓缩前缀 30 字（`_CHILD_PREFIX_MAX`） |
| 经历片段 | 不含 `tech_stack` | 含 `tech_stack` |

两臂 `keyword_index_text` 完全一致（抽样 150 人 150/150 相同），因此 FTS 词法面、硬过滤字段与 reranker 证据文本都没有变，差异只落在「喂给 embedding 的 `vector_text`」。

**一个必须说清的定位缺口**：本实验只能证明「v9 这个整包更差」，**没有**分辨是哪一处改动（父字段增删 / 技能限长 / 子前缀变短 / 经历加技术栈）导致的退化。逐字段定位要对每个变体各重建一次全量索引，按实测约 7 文本/秒，每个变体约 1 小时机时，因此列为下一轮待办，本轮不给结论。

**为什么 v9 反而更差**（未验证假设，仅供下一轮定位参考）：v9 的父向量剔除了城市/学历/年限/公司枚举并压缩了技能枚举，设计意图是「去掉对语义无用的字段、让向量更聚焦业务」。反向结果的可能机制有三条：① 城市、公司这类专名是**高区分度锚点**，删掉后候选人的父向量都退化成「业务方向 + 技能名」的相似文本，点与点之间挤在一起、最近邻竞争变弱（旁证：两臂 top-10 只有 66% 重合，候选池整体偏移）；② 技能截断到 24 条，对技能面宽的资深候选人砍掉的正是信息量最大的部分；③ 父向量的主干变成 LLM 生成的画像，本身就高度同质。hybrid 臂不受影响是因为它还有 FTS（词法面完全相同）与 reranker（证据文本相同）两路兜底，向量只是融合的一路。若确认，修法应是「父向量保留技能标准名上限、只剔除硬字段」，而不是全盘回退。

## 方案选择

### 方案 A：共享向量索引，按模式区分查询、配额和融合（采用）

候选人 `parent`、`profile_point`、`experience`、`project` 四种语义文本只生成和嵌入一次。纯向量与混合模式在读侧选择不同的查询组合、chunk 类型配额和融合策略；重排证据按候选人聚合。

优点：索引一致、重建成本可控、两种模式可共享消融结果，也能通过读侧策略表达不同目标。缺点：需要扩展内部命中契约，保留多条证据与各通道原始信号。

### 方案 B：纯向量和混合各维护一套向量文本及索引（不采用）

优点是两种模式可以独立优化。缺点是 embedding、存储、重建、增量同步和版本回滚成本近乎翻倍，同一候选人在两种入口中更容易产生难以解释的差异。目前没有证据证明模式差异必须靠两套文档表达。

### 方案 C：保持索引不变，只修改 AI 提示词和阈值（不采用）

实施快，但无法解决已保留子片段未被搜索重排使用、正文开关失效和 RRF 原始分数丢失。已有分数分布显示相关与不相关样本高度重叠，继续单独调绝对阈值不能解决根因。

## 目标架构

```text
                         ┌─ 原关键词 ──→ FTS（原词 + 确定性词典别名）
用户查询 → 规则解析 ─────┤
                         ├─ 原关键词 ──→ query embedding A
                         └─ 保真规范化 → query embedding B（可选）
                                              │
候选人共享向量索引：parent / profile_point / experience / project 分类型召回
                                              │
       FTS rank+score ─┐                       │ vector rank+score+chunk
                       ├─→ 候选人级融合与证据聚合
       原查询向量 ─────┤
       规范化向量 ─────┘
                                              │
                       概况 + 查询主证据 + 查询补充证据
                                              │
                                  原查询 → reranker
                                              │
                                     排序、截断、解释
```

## 三种文本面的职责

### 1. FTS 文本

`keyword_index_text` 继续承载标准名、确定性别名和可检索字段。技能别名由 `search/lexicon.py` 统一展开，AI 不参与 FTS 同义词生成。

默认只查父文档概况；`search_body=true` 时把经历/项目正文纳入 FTS。这个开关在 keyword 和 hybrid 两种模式中必须具有相同含义。

### 2. Embedding 文本

候选人侧保留四种原始 `kind`：`parent`、`profile_point`、`experience`、`project`。四者的长度、语义粒度和数量分布不同，因此初始方案按四种 kind 独立召回；原始 `kind` 不因召回或重排用途而改变。

#### Parent

包含：

- 职业定位或主要交付类型；
- 有来源依据的核心能力摘要；
- 结构化业务方向；
- 去重并限长后的核心技术概览；
- 最近岗位名称，仅在它能解释职业定位时保留。

排除：

- 姓名、手机号、年龄；
- 城市、学历、学校、QS、工作年限；
- 单纯用于精确筛选的公司枚举；
- AI 无来源推断出的技能、行业或岗位层级。

#### Profile point

每条有来源路径的结构化画像分点独立成向量，用于承载跨经历归纳但仍可回查的能力、职责或业务事实。只保留该分点自身及必要的短职业定位上下文，不复制完整父画像，也不把它改标为 `experience` 或 `project`。每条继续保留 `kind=profile_point`、`sequence` 和 `evidence_path`。

#### Experience

每段工作经历独立成向量，包含岗位、主要职责、实际技术栈和业务场景。公司名只在能够说明业务背景时保留。不得把通用父画像前缀复制到每个子片段；如实验需要上下文，只允许加入一条不超过 30 个中文字符的职业定位前缀，并把“无前缀”作为对照组。

#### Project

每个项目独立成向量，包含有语义的项目名称、业务场景、本人职责、技术栈和结果。删除联系方式、教育、地点、年限及与项目无关的通用画像。

所有文本从现有结构化简历确定性构造，不新增 LLM 调用；每个片段保留原始 `kind`、`sequence` 和 `evidence_path`，支持结果解释和回查。

### 3. Reranker 证据文本

当前工作区的召回层已经通过 `EvidenceChunk(kind, text, score)` 和 `select_evidence()` 为每个 `SearchHit` 附带最多三条不同 kind 的片段，match 侧也已经用结构化 JD/候选人字段构造技术/业务证据包。但“不同 kind”只是当前实现，不是目标规则；它会让 `profile_point` 的用途不清晰，并阻止两条同 kind 的查询相关证据同时保留。搜索 reranker 也尚未消费这份证据，仍只读取 `hit.vector_text or hit.content`。

本设计复用并扩展现有 `EvidenceChunk`，不再引入一套平行的证据类型。重排文本不再直接等同于单条 `vector_text`，而是从现有证据中为每个候选人组装一份只读证据包：

```text
[概况]
优先使用真实 parent；没有 parent 时才使用排名最高的代表片段

[查询主证据]
其余片段中查询概念覆盖最强的一段

[查询补充证据]
其余片段中对主证据未覆盖概念补充最多的一段
```

搜索侧不再要求把自由查询硬分成“技术概念”和“业务概念”。当前 `search/lexicon.py` 只明确维护技能和少量通用同义词；行业桶与业务方向标签位于其他模块，覆盖范围和语义层级不同。把它们临时拼成二分类会让“后端”“平台治理”“数据架构”等查询得到不稳定标签。

搜索证据选择直接使用 `parse_query()` 去掉硬条件后的全部 `LexicalConcept`：每个概念沿用已有 alias 集合，不新增生成模型或新的技术/业务分类词表。只排除固定的低信息功能词和职责动词（如“负责、熟悉、具备、经验、相关、等”），其余已策展和未策展概念同等参与。概念 alias 与证据文本统一经过 `tokenize_lexical_text()`，再对每个非 parent 片段计算命中的查询概念下标集合：

1. 主证据先最大化查询概念覆盖数，再按片段的融合 contribution 和稳定 chunk ID 打破平局。
2. 补充证据先最大化相对主证据的新增概念覆盖数，再比较总覆盖数、融合 contribution 和稳定 chunk ID。
3. 若片段没有词法覆盖但通过现有向量阈值进入召回，仍可按融合 contribution 参与兜底；不得仅因词表未收录自然语言职责就丢弃向量证据。
4. 两个槽位要求规范化文本不同，但不要求 kind 不同；两个不同的 `profile_point`、experience 或 project 都可以同时入选。

这里的证据选择覆盖与 H3 的候选人级 `concept_coverage` 不是同一指标：后者只统计已策展概念，用于可比较的软排序特征；前者允许未策展自然语言概念参与，只决定给 reranker 看哪些文本，不直接加减候选人融合分。

最多保留三个**语义槽位**，缺少相关证据时留空，不拿无关片段补位。候选人侧的 `profile_point`、`experience`、`project` 和 JD 侧的 `child` 都按上述查询相关性竞争主证据与补充证据。

match 流程是另一种上下文：它已有结构化 `required_skills`、`business_directions`、requirement label 以及 `match/keywords.py` 的 `build_*_tech_terms()` / `build_*_biz_terms()`，因此可以继续渲染“技术证据/业务证据”。搜索和 match 共用原始 `EvidenceChunk`、去重、通道信号与稳定 parent 规则，但不强求共用槽位名称或概念分类器。

方案取舍：把技能 lexicon 与行业/业务方向词表拼成技术/业务二分类，边界不完整且会产生多标签歧义；把所有已策展概念都算技术则会把“后端”等方向词强行归类；只取 rank 最高的两段虽然简单，却可能得到两段覆盖同一概念的重复证据。因此采用“全部有效查询概念 + 主证据覆盖 + 补充证据边际覆盖 + 向量兜底”，不解决本阶段并不需要解决的技术/业务分类问题。

证据进入槽位后仍保留原始 `kind`、`sequence`、`evidence_path` 和全部通道信号，不能把 `profile_point` 重标为 `experience`、`project` 或 `parent`。只有真实 `kind=parent` 才能在内部契约中标为 parent；缺少 parent 时，固定通道优先级选出的片段可以作为 `overview` 回退，但其原始 `kind` 不变。相同文本按规范化内容去重，总长度上限 1200 个 Unicode 字符，超限时按“查询概念覆盖数 → 片段 RRF 贡献 → 稳定 chunk ID”裁剪。

### 4. 代表字段与候选人聚合

混合检索不再选一条“赢家 row”同时承担排序、展示和重排。`SearchHit` 被定义为**候选人级聚合对象**：排序由各通道信号决定，reranker 读取语义证据包，`chunk_id/content/vector_text` 只作为兼容性的稳定展示投影。

代表字段采用以下不变量：

1. `candidate_id/revision_id` 是聚合主键，不从某个通道的赢家 row 推导。
2. `chunk_id/content/vector_text` 优先取该 revision 的真实 `kind=parent`。parent 未出现在召回行时，对最终候选池按 revision 批量回读 parent，不能因为 BM25 或 vector 先完成就改变代表字段。
3. 索引异常导致 parent 确实缺失时，才按固定优先级 `bm25 → vector_original → vector_rewrite` 取各通道内部排名第一的片段作为展示回退，同时保留其真实 kind 并记录 `PARENT_CHUNK_MISSING`；该回退不参与融合加分。
4. `matched_channels` 是候选人在各通道是否出现的并集；`score` 是最终公开排序分。两者都不能从代表行反推。
5. `vector_text` 不再作为 reranker 输入，只保留迁移期兼容；搜索重排统一使用 overview/primary/complementary 证据包，match 复核可继续使用结构化的 overview/tech/business 标签。

这一定义刻意不采用“BM25 rank 与 vector rank 谁小谁代表”的方案。两种 rank 只表示各自通道内的位置，数值不能直接比较；而且代表字段改变会连带改变 API `content` 以及当前由 `hit.content` 生成的“关键词命中”理由。

API 兼容口径固定为：`content` 返回 parent 概况文本，`reasons` 的命中依据改从已选证据及其原始 kind 生成，不再对 `content` 做字符串包含判断。当前前端只声明 `content` 类型、没有直接展示该字段；仍需用 API 契约测试锁住它，未来如需显示命中片段，应新增明确的脱敏 `evidence_summary`，不能复用 `content` 的含义。

方案取舍如下：

- “跨通道 rank 最小的 row 当代表”：拒绝，rank 语义不可直接比较，且公开字段会随排序策略漂移。
- “继续 BM25-first，只让 reranker 读取证据包”：可以作为最小补丁消除当前主要排序伤害，但 vector-only 候选仍会把 child 暴露为 `content`，接口语义不稳定。
- “候选人聚合 + parent 稳定投影 + 独立证据包”：采用。它多一次候选池级 parent 批量回读，但把排序、解释和公开字段的职责彻底拆开，后续调整融合权重不会改动 API 展示语义。

## 两种模式的共用与差异

### 纯向量模式

目标是最大化语义召回并保持结果可解释：

1. 原关键词始终生成查询向量 A。
2. AI 改写成功且通过验证时生成查询向量 B；A 和 B 分别召回，不能用 B 替换 A。
3. 对 `parent`、`profile_point`、`experience`、`project` 四种 kind 分类型检索，避免长度、数量和语义粒度不同的文档只在一个全局 top-K 中竞争。
4. 每个查询变体、每种 kind 先取 40 行，合并后按候选人保留各类型最佳证据；候选池最终封顶 120 人。
5. 每种查询变体先把四种 kind 的候选人 reciprocal-rank contribution 取最大值；原查询与改写查询再按 vector family 规则取最大值。这样 `profile_point`、经历或项目任一强证据都能召回候选人，但片段多、类型全不会天然获得重复加分。
6. reranker 使用原关键词和候选人证据包；改写词只作为辅助信息记录，不作为唯一重排查询。

`40 × 4` 指四种原始 `kind`，是首轮实验档而非生产定档。实验同时保留当前“全类型一次取 100”作为基线，并比较分类型 `20/40/60` 三档；最终候选人数仍在按候选人合并去重后封顶 120，不是返回 160 人。

### 混合模式

目标是让 FTS 负责精确命中，向量负责职责、项目场景和表达差异：

1. FTS 使用原关键词和确定性别名，不使用 AI 生成的词表。
2. 向量通道使用与纯向量相同的共享索引及双查询策略。
3. FTS 与向量分别按候选人保留最佳 rank、原始 score、最佳 chunk 和补充证据。
4. 概念覆盖率作为软排序特征，不设置新的“一票否决”过滤。覆盖率定义为“命中的已策展查询概念数 / 查询中已策展概念数”；查询没有已策展概念时记为 `None`。
5. 跨通道采用 weighted RRF 做第一轮实验，权重矩阵为：
   - 基线：FTS 1.0 / vector 1.0；
   - 词法偏重：FTS 1.25 / vector 1.0；
   - 语义偏重：FTS 1.0 / vector 1.25。
6. 在没有独立验收集收益前，不切换当前等权 RRF。
7. reranker 输入仍为原查询和候选人证据包，不使用同义词大串。

## AI 语义改写设计

### 定位

AI 改写只负责保真规范化，不负责岗位知识联想。已知别名优先由词典确定性处理；LLM 主要处理未覆盖的缩写、混合中英文和自然语言短句。

推荐提示词：

```text
你是招聘人才搜索查询的保真规范化器，不是联想扩写器。

任务：在不改变原查询意图、范围和强弱关系的前提下，生成一条简洁的语义检索查询，供向量模型使用。

规则：
1. 必须保留原查询中的每个岗位、技能、行业和业务概念。
2. 只允许规范明确且无歧义的缩写或别名，例如 JS→JavaScript、K8s→Kubernetes、Golang→Go、数仓→数据仓库。
3. 每个概念最多保留一个标准名和一个常用别名。
4. 禁止根据岗位名称推断并新增技能、框架、职责、行业、资历或业务场景。
5. 禁止添加城市、学历、年限、学校、公司、薪资、年龄、性别等条件。
6. 如果原查询已经清楚，原样返回。
7. 不要输出 OR、解释、括号说明、分类标签或关键词清单。
8. 输出不得超过原查询长度的 2 倍，且最多 80 个字符。
9. 只返回 JSON：{"semantic_query":"..."}

示例：
“JS 后端” → “JavaScript 后端开发”
“K8s 微服务” → “Kubernetes 微服务”
“数仓 TL” → “数据仓库 技术负责人 TL”
“核心交易系统 开发” → “核心交易系统 开发”
“软件工程师 后端” → “软件工程师 后端开发”
```

### 输出校验

改写结果必须同时通过：

1. 非空，最多 80 个字符，且长度不超过原关键词的 2 倍；原词不足 10 个字符时允许最多 20 个字符。
2. 不含 `OR`、`AND`、括号式解释、“同义词：”“技能术语：”等列表标记。
3. 不引入新的硬过滤字段。
4. 原查询中的已策展概念经 canonical 化后必须全部保留。
5. 改写不得新增原查询中不存在的已策展技能概念；标准名和同组别名视为同一概念。
6. 任一校验失败时保留原查询，不产生查询向量 B。

缓存键增加独立的 `REWRITE_PROMPT_VERSION`，避免提示词升级后继续命中旧缓存；不能再用 `LEXICON_VERSION` 间接代表提示词版本。

不把现有四值 `rewrite_status` 扩成六值，而是采用分层契约：

- `rewrite_status` 保持 `disabled | not_applicable | success | unavailable`，表示功能是否启用、适用和可用，避免破坏现有 API 与 desktop 类型。
- `rewrite_applied: bool` 表示是否真的生成并采用了查询向量 B。
- `rewrite_fallback_reason: unchanged | rejected | provider_error | None` 表示未采用改写的具体原因，只用于诊断、评测和可选的高级展示。

状态映射固定如下：

| 场景 | rewrite_status | rewrite_applied | rewrite_fallback_reason | semantic_query |
| --- | --- | ---: | --- | --- |
| 用户未开启 | `disabled` | false | `None` | `None` |
| 关键词模式或空查询 | `not_applicable` | false | `None` | `None` |
| 生成不同且通过校验的规范化查询 | `success` | true | `None` | 规范化查询 |
| 合法输出与原查询规范化后相同 | `not_applicable` | false | `unchanged` | `None` |
| 收到合法文本但安全/保真校验拒绝 | `not_applicable` | false | `rejected` | `None` |
| 模型、网络、超时、JSON/schema 解析失败 | `unavailable` | false | `provider_error` | `None` |

`unchanged` 和 `rejected` 均静默使用原查询；只有公开状态为 `unavailable` 时延续现有“AI 改写不可用”提示。`success` 继续保持“`semantic_query` 必定非空且确实用于查询向量 B”的既有不变量，避免旧客户端看到 `success + semantic_query=None`。这样旧客户端仍只处理四值枚举，新版评测又能区分“无需改写”“校验拒绝”和“服务故障”。

方案取舍：公开枚举直接扩成六值会形成前后端破坏性契约变更；只在后端把细状态压回四值会丢失 A/B 诊断；跳过阶段 2 则无法解决当前“改写波动大但收益不稳定”的核心问题。因此采用“四值公开状态 + applied + fallback reason”的分层方案，新增字段对旧 JSON 客户端是向后兼容的，desktop 现有提示逻辑无需变化。

## 内部检索契约

为避免给外部 API 暴露索引细节，新增的通道信号只在搜索模块内部流转。当前工作区已经存在 `EvidenceChunk(kind, text, score)` 以及 `SearchHit.evidence`，因此在其上向后兼容地扩展，而不是新增一套平行的 `RetrievalEvidence`。同一 chunk 可能同时命中多个通道，所以不能只放一组 `channel/rank/raw_score`；改为给 chunk 挂载通道信号列表：

```python
@dataclass(frozen=True, slots=True)
class ChannelSignal:
    channel: str             # bm25 / vector_original / vector_rewrite
    rank: int                # 仅在本召回列表内有意义；向量列表还按 kind 拆分，从 1 开始
    raw_score: float | None  # BM25 或 vector similarity，不跨通道直接比较
    reciprocal_rank: float   # 1 / (rrf_k + rank)，权重与通道族聚合在上层计算

@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    kind: str
    text: str
    score: float = 0.0    # 保留现有字段，旧调用方继续可用
    chunk_id: str | None = None
    sequence: int | None = None
    evidence_path: tuple[str, ...] = ()
    signals: tuple[ChannelSignal, ...] = ()
```

`raw_score` 的含义由 `channel` 决定：BM25 保存 `_score`，向量保存经统一公式换算后的 higher-is-better similarity。两者只用于本通道阈值、诊断和后续校准，不能直接相加或比较大小。现有 `EvidenceChunk.score` 在 vector 行上会因为只读取 `_score` 而变成 0，因此新融合逻辑不得继续依赖这个含义不明确的字段；它只作为旧 match 证据选择逻辑的兼容字段保留。

`SearchHit` 增加内部字段：

```python
# 已存在，元素扩展为上面的 EvidenceChunk
evidence: tuple[EvidenceChunk, ...] = ()
representative_kind: str = "parent"
bm25_rank: int | None = None
bm25_score: float | None = None
vector_original_rank: int | None = None
vector_original_score: float | None = None
vector_rewrite_rank: int | None = None
vector_rewrite_score: float | None = None
fusion_score: float | None = None
concept_coverage: float | None = None
```

`QueryPlan` 保留现有 `rewrite_status` 四值枚举并增加：

```python
rewrite_applied: bool = False
rewrite_fallback_reason: str | None = None  # unchanged / rejected / provider_error
```

服务层内部的 `RewriteResult` 可以使用更细的 `changed/unchanged/rejected/unavailable` outcome，但映射到 `QueryPlan` 和 API 时必须按上表收敛，不能把内部 outcome 直接塞进公开 `rewrite_status`。

混合融合只使用通道内 rank，先对每个“查询变体 × kind”列表计算 `rr=1/(rrf_k+rank)`。同一查询变体下，候选人的四种 kind contribution 取最大值，不按命中片段数量累加。原查询向量与改写向量保留为两个独立 signal，但归入同一个 vector family，向量贡献取 `max(vector_original_contribution, α × vector_rewrite_contribution)`，避免同一候选人因同时命中原查询和改写查询而获得双倍向量权重；首轮 `α=1.0`，后续只通过验收集校准。最终融合分为 `w_fts × rr(bm25) + w_vector × vector_family_contribution`。这是先把各列表内位置转成 reciprocal-rank contribution，再在固定权重的通道族间融合，不是直接比较 BM25 rank 和 vector rank。阶段 1 保持现有等权 `w_fts=w_vector=1.0`，不顺带调参。

同一候选人或 chunk 在多通道命中时仍保留所有 signal。若以后要引入 raw score 融合，必须在每个通道内按固定评测集做 percentile/校准后再实验，不能在本阶段临时 min-max。

`select_evidence()` 继续作为搜索与 match 共用的底层入口，负责把行转换成带 `ChannelSignal` 的去重 `EvidenceChunk`，但不在底层硬编码技术/业务分类。搜索适配器接收 `ParsedQuery.concepts`，按 overview/primary/complementary 规则选择；match 适配器接收结构化 tech/biz terms，按 overview/tech/business 渲染。两者都不能依赖“BM25 列表先遍历”决定代表证据，也不能再用“kind 必须不同”代替文本去重。概念覆盖相同的片段使用与候选人融合一致的“FTS contribution + vector family 最大 contribution”排序，仍相同时按稳定 chunk ID；不直接比较跨通道 raw rank 或 raw score。槽位分配只是对 `EvidenceChunk` 的只读引用视图，原始 chunk 不改 kind。`EvidenceChunk(kind, text, score)` 的旧构造方式保持兼容。

外部响应继续保留当前 `score` 和 `matched_channels`；`rerank_score` 仍是内部字段，成功时按现有行为写入公开 `score`。`content` 固定为稳定 parent 投影，只有需要解释时才把经脱敏的证据摘要映射到 API，不直接返回内部 rank/raw score/fusion score。

## 正确性与降级策略

1. FTS 失败：纯向量结果继续返回，并标记 `FTS_UNAVAILABLE`。
2. 原查询 embedding 失败：向量模式返回服务错误；混合模式降级为 FTS。
3. 改写或改写 embedding 失败：丢弃查询向量 B，继续使用原查询向量 A；不得让可选改写拖垮搜索。
4. reranker 失败：按 `fusion_score` 顺序返回。因为聚合结果分别保留原查询向量和改写向量的通道内 score，混合模式可以执行经评测确定的向量质量兜底，不再完全失去原始相似度。
5. 某种 chunk 不存在：用其他类型补足候选池；某个证据槽没有相关片段：该槽留空，不用无关文本伪造证据。
6. `search_body` 只影响 FTS 是否检索经历/项目正文，不影响向量通道的 child chunk；UI 文案必须说明这一语义。
7. 内部召回池与请求 `limit` 解耦：普通请求固定候选池 120；`limit` 只控制最终返回。超过 120 的深翻页请求按显式深分页策略扩池，不反向改变第一页排序。
8. parent 批量回读失败：保留融合结果，使用固定通道优先级的展示回退并记录 `PARENT_CHUNK_MISSING`；不能改用跨通道 rank 大小决定代表行。

## 分阶段改动

### 阶段 0：评测和可观测性

- 把改写 A/B 改为调用生产 `/api/search/candidates` 入口，覆盖 `parse_query`、硬条件合并、查询计划和 hydration。
- 每次搜索记录脱敏诊断：原关键词、规范化查询哈希、各通道召回数、阈值前后数量、chunk 类型分布、融合池大小、reranker 状态与各阶段耗时。
- 冻结数据库、索引、embedding 模型、reranker 模型和查询集；调参集与最终验收集分开。

### 阶段 1：确定性读侧修复

- 将 `search_body` 传入 `_parallel_retrieve()`。
- 解耦请求 `limit` 与内部候选池（前端已无 limit 入口，本项服务 API 调用方、脚本与深分页）。
- 在现有 `EvidenceChunk` / `SearchHit.evidence` 上扩展多通道 signal 和证据路径；`SearchHit` 分别保存各通道 rank/raw score 与 `fusion_score`，不新增第二套证据类型。
- 把 `_rrf()` 改为显式候选人聚合：weighted RRF 只使用各通道内 rank；代表字段从真实 parent 批量投影，不参与融合，也不再强制证据 kind 各不相同。
- 修正 match 侧真实 parent/overview 规则，保留其结构化 overview/tech/business 语义槽；另为自由搜索构造 overview/primary/complementary 证据包并接入 reranker，替代单条 `vector_text`。
- API `content` 固定为 parent 概况，`reasons` 改从证据槽生成；保持现有前端展示不依赖 `content`，新增接口断言防止字段语义漂移。

本阶段不改索引文本、不调用 AI 改写、不调整权重，便于隔离验证确定性修复。

### 阶段 2：保真改写与双查询召回

- 替换提示词并增加校验、prompt version 和内部 outcome；公开 `rewrite_status` 维持四值，新增 `rewrite_applied` 与 `rewrite_fallback_reason`。
- 原查询向量 A 始终存在；改写向量 B 只作补充。
- reranker 改用原查询。
- 改写默认保持关闭，先完成扩大样本 A/B。

### 阶段 3：共享向量文本 v9

- 实现新的 parent、profile_point、experience、project 构造函数和长度约束。
- 不新增持久化列，继续复用现有 `kind`、`sequence`、`evidence_path`、`vector_text`。
- `INDEX_SCHEMA_VERSION` 保持 `10`，`INDEX_CHUNK_VERSION` 从 `8` 升至 `9`。
- 在隔离索引中同时构建旧版 v8 与新版 v9，使用同一 embedding 模型和同一快照做消融；验收前不切换生产索引。

### 阶段 4：分类型召回与融合校准

- 比较全局 top-100 与 parent/profile_point/experience/project 分类型 `20/40/60` 三档；分别记录四种 kind 的召回占比、候选人去重后贡献和有效证据率。
- 比较三组 weighted RRF 权重。
- 比较概念覆盖率只用于 tie-break、作为轻权重特征、完全不用三种方案。
- 只在独立验收集满足门槛后固化参数（2026-09-20 在调参集上 8 档均无可信收益，故未固化任何参数，保持基线默认）。

### 阶段 5：灰度与切换

- shadow 构建 v9 索引，完成兼容性、行数、随机样本、删除与增量同步检查。
- 先灰度读流量并保留 v8 回退入口，再按现有 rebuild maintenance 流程切换。
- 切换后保留旧索引一个完整观察周期；出现质量或兼容性门槛失败时恢复 v8。

## 文件影响范围

| 文件 | 责任 |
| --- | --- |
| `backend/src/kerui_recruit/search/contracts.py` | 增加 `ChannelSignal`，扩展现有 `EvidenceChunk` 和 `SearchHit` 的多通道信号与稳定代表 kind；保持旧构造方式兼容；为 `QueryPlan` 增加 applied/fallback 字段但维持四值 status。 |
| `backend/src/kerui_recruit/search/rewrite.py` | 保真提示词、输出校验、prompt version、缓存与内部 `changed/unchanged/rejected/unavailable` outcome。 |
| `backend/src/kerui_recruit/search/service.py` | 双查询向量、正文开关透传、固定内部池、基于全部查询概念的 primary/complementary 证据包重排、内部 outcome 到四值公开 status 的映射和降级。 |
| `backend/src/kerui_recruit/search/lancedb_index.py` | 四种 kind 的分类型向量召回、候选人级 RRF、parent 批量投影、证据聚合、保留各通道 rank/score、chunk v9。 |
| `backend/src/kerui_recruit/search/documents.py` | parent/profile_point/experience/project v9 文本构造。 |
| `backend/src/kerui_recruit/search/sync.py` | 使用 v9 文档构造并保持增量同步一致。 |
| `backend/src/kerui_recruit/match/service.py` | 修正 parent/overview 选择；继续使用结构化 tech/business terms 构造 match 专用证据包。 |
| `backend/src/kerui_recruit/match/review.py` | 按 match 专用槽位渲染复核 prompt，不与自由搜索槽位名称耦合。 |
| `backend/src/kerui_recruit/api/search.py` | `rewrite_status` 继续使用四值 Literal，增加 `rewrite_applied` / `rewrite_fallback_reason`；固定 `content` 为 parent 概况，`reasons` 改从证据生成。 |
| `desktop/src/App.tsx` | `rewrite_status` 类型维持四值；可接收新增的可选诊断字段，但只有 `unavailable` 显示服务不可用提示；保持 `content` 不直接展示。 |
| `backend/tests/search/*` | 文档构造、融合、证据保留、改写、降级和索引版本测试。 |
| `scripts/*retrieval*` / `scripts/*rewrite*` | 生产入口 A/B、隔离索引消融、指标与诊断产物。 |

## 测试与评测设计

### 单元与契约测试

必须覆盖：

1. hybrid 的 `search_body=false/true` 与 keyword 模式范围一致。
2. 同一候选人 BM25 命中父行、向量命中 child 时，`chunk_id/content/vector_text` 稳定取真实 parent，`matched_channels` 同时包含两路，证据保留两条及各自 `ChannelSignal`，搜索 reranker 能看到 child。
3. 现有 `EvidenceChunk(kind, text, score)` 构造方式保持兼容，新增字段均有默认值；match 侧 `_evidence_pack()` 修正真实 parent/overview 规则并继续使用结构化 tech/business terms。
4. 搜索侧用全部 `ParsedQuery.concepts` 选择 primary/complementary：主证据覆盖最多，补充证据优先覆盖主证据未覆盖的概念；两个不同的同 kind 片段可以同时入选。
5. 查询只有未策展自然语言概念或片段只有向量命中时，仍能按融合 contribution 选择证据，不因缺少技术/业务标签而清空证据包。
6. 一个候选人拥有大量 child 时不会挤占其他候选人的名额。
7. `limit=20/50/100` 在相同查询、相同固定候选池下，公共前 20 的顺序一致（API 层契约：前端已不再传 `limit`，该差异只能由 API 调用方与脚本触发）。
8. 原查询向量成功、改写服务失败时仍返回原查询结果，映射为 `unavailable / applied=false / provider_error`，desktop 只在此场景显示不可用提示。
9. 改写有效变化映射为 `success / true / None`；无变化映射为 `not_applicable / false / unchanged`；安全拒绝映射为 `not_applicable / false / rejected`。三者的公开 `rewrite_status` 不出现新枚举值，`success` 仍保证 `semantic_query` 非空，后两者静默使用原查询。
10. “软件工程师 后端”不得新增微服务、Redis、高并发等概念。
11. “JS 后端”“K8s 微服务”“数仓 TL”正确规范化。
12. 改写含新城市、学历、年限、技能或 `OR` 列表时被拒绝。
13. reranker 失败后，原始 BM25、原查询向量和改写向量信号仍可用于融合结果解释和兜底。
14. 两个查询相关的 `profile_point` 可分别进入搜索 primary/complementary；match 流程仍可按结构化 terms 分别进入 tech/business。两种流程均保留 `kind=profile_point` 与来源路径；缺少真实 parent 时 overview 回退不伪装成 parent。
15. 交换 BM25/vector 任务完成顺序或 `_rrf()` 通道遍历顺序，不改变 `chunk_id/content/vector_text`、`matched_channels`、融合分和证据槽；vector-only child 命中也会批量回读 parent 作为稳定投影。
16. BM25 rank=1 与 vector rank=1 不做原始数值比较；断言 weighted RRF 分别计算通道 contribution，原查询与改写查询命中同一候选人时 vector family 取最大值而非双重累加，并在相同输入下稳定排序。
17. API `content` 始终是 parent 概况；查询词只出现在 child 时，`reasons` 仍能从证据生成正确命中依据。前端不把 `content` 当成命中片段展示。
18. v8 索引被识别为需要重建，v9 重建后兼容性通过。

### 查询集

从现有真实查询扩展到 60–100 条，并按以下桶分层：

- 精确技能与岗位名；
- 缩写及中英文混合；
- 模糊短查询；
- 自然语言职责；
- 行业/业务场景；
- 带城市、学历、年限、排除项的硬条件查询；
- 容易漂移的宽岗位词，如“软件工程师”“架构师”“负责人”。

同一查询的各变体结果合并去重后一起盲评，避免候选人在不同实验臂被重复且不一致地判分。

### 指标

- Recall@20/50/100；
- Precision@5/10；
- nDCG@10；
- 首位合格率；
- 强相关 grade=3 召回数；
- 无结果正确率；
- top-10 集合 Jaccard 与排序变化率；
- 硬条件违规数；
- p50/p95 总延迟及各阶段延迟；
- 每次搜索 embedding/rerank 调用数与 token/字符数；
- 改写公开 `rewrite_status` 分布、`rewrite_applied` 比例及 `rewrite_fallback_reason` 分布；
- 各 chunk 类型进入召回池、证据包和最终 top-10 的比例。

## 发布门槛

### 确定性修复

- `search_body` 在 keyword/hybrid 两模式语义一致，相关契约测试 100% 通过。
- 同一查询在不同 `limit`（API 传入 20/50/100）下公共前 20 完全一致；前端已不暴露该入口。
- 硬条件违规数为 0。
- provider 降级路径全部有明确状态且不丢失仍可用的通道结果。

### 向量文本 v9

- 相对 v8，独立验收集 Recall@50 至少提升 5%（相对值）。
- vector nDCG@10 不下降超过 0.01。
- hybrid nDCG@10 与 P@5 均不下降超过 0.01。
- p95 索引检索阶段延迟不增加超过 20%。
- 未同时满足时继续使用 v8，不因完成重建而切换。

### AI 改写

保持可选、默认关闭的最低条件：所有硬条件和新增概念安全测试通过，hybrid nDCG@10/P@5 不显著退化。

2026-09-20 实测判定：hybrid nDCG@10 由 0.4491 降到 0.4174（Δ−0.0317，逐查询胜负 13:29，P@5 由 0.4533 降到 0.4033），已构成显著退化 → **最低条件不满足，维持默认关闭**。这也说明当前「双查询 + 等权融合」的写法本身有问题（vector 单模式仅 +0.0137 且胜负持平），下一步应先做消融（改写臂权重、只召回不融合）而不是换提示词。

改为默认开启必须同时满足：

- hybrid nDCG@10 至少绝对提升 0.02；
- 逐查询配对检验 `t > 2`；
- P@5、首位合格率、grade=3 召回均不下降；
- p95 总延迟增幅小于 15%；
- `rewrite_fallback_reason` 为 `rejected` 或 `provider_error` 的合计比例低于 5%。

未满足任一条件时保持默认关闭。

## 回滚与版本管理

1. v9 使用独立索引目录构建，v8 全程保持只读可用。
2. 索引元数据明确记录 schema 10、chunk 9、embedding 模型和文档构造版本。
3. 读侧改造拆成独立提交：正文开关、证据契约、AI 改写、向量文档、融合参数不能混为一次不可分割发布。
4. AI 改写可通过现有前端开关立即关闭；双查询失败自动退回原查询单路。
5. 融合权重保留等权 RRF 配置，可在不重建索引的情况下回退。
6. 发现质量回归时先回退读侧权重/改写，再回退 v9 索引，避免同时改变多个变量。

## 非目标

- 不在本轮更换或微调 BGE-M3 / reranker 模型。
- 不为两种搜索模式建立两套候选人向量索引。
- 不把 AI 生成同义词写入 FTS 索引。
- 不把结构化硬条件变成向量相似度条件。
- 不扩大匹配业务规则、JD 解析或方向分类范围。
- 不凭单次人工观察直接修改生产阈值或融合权重。

## 最终验收产物

实施完成后必须交付（✅ 已产出 / ⏳ 待补）：

1. ✅ v8/v9 同快照向量文本与召回消融报告（`scripts/retrieval_ablation_2026_09_20.py --suite index`）：`.tmp-ablation/index-v8v9.json`，两臂同一快照、同一 embedding 模型、各 30,671 chunk（结论见上表）；
2. ✅ keyword/vector/hybrid 三模式指标及逐查询差异：`.tmp-ablation/readside-modes.json`（基线档位、同一 60 条查询、含逐查询 top-10 与 Jaccard）；vector / hybrid 另在 `.tmp-ablation/readside.json` 的 `baseline` 臂中与 8 档位同批采集；
3. ✅ AI 改写开关 A/B、失败类型和延迟成本报告：`.tmp-ablation/rewrite-ab.json`，含六值状态分布、逐查询 top-10、Jaccard、延迟与分模式判决指标；
4. ✅ chunk 类型与证据包覆盖率报告：`.tmp-ablation/readside.json` 的逐档 `kind_distribution` 与 `metrics`；
5. ✅ 索引重建记录：`index-rebuild.json` 记录 `mode=inplace / total=1542 / finished_at`，`outbox pending=0`；增量同步由同一 outbox 机制覆盖；切换与回滚演练未做（属运维动作）；
6. ✅ 完整后端测试、前端测试和构建结果：后端 `pytest -q` 全绿、前端 `tsc -b` + `vitest run` 全绿 + `vite build` 成功。

读侧档位与改写开关的**定档**仍缺第 2 步之外的独立验收集；在它建立之前，一律保持基线默认（等权 RRF、全局召回、改写默认关闭）。
