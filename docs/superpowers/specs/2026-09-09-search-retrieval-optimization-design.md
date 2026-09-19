# 候选人检索优化设计

## 1. 目标

在不改变现有精确筛选语义、不破坏关键词/向量/混合三种模式边界的前提下，完成四项改造：

1. 用统一的词法概念和词典取代中文 ngram 跨词匹配。
2. 让技能、学历和学校别名在文档侧与查询侧使用同一来源。
3. 仅在关键词模式提供可解释的 smart/and/or 逻辑。
4. 向量和混合模式可由用户显式开启 LLM 语义改写，且任何失败均回退到原查询。

## 2. 非目标

- 不让 LLM 改写关键词 FTS 查询。
- 不让 AND/OR 改变精确筛选、排除技能或候选人状态的逻辑。
- 不向 `vector_text` 重复注入全部别名。
- 不以“返回结果更多”作为质量验收标准。
- 本轮不改造 JD 匹配与候选人反向匹配的业务语义；新参数默认关闭，它们的现有行为必须保持。

## 3. 模式契约

### 3.1 关键词模式

- 只调用 FTS，不调用 embedding、reranker 或 LLM 改写。
- `smart`：保留当前的相关性排序，不做额外硬布尔过滤。
- `and`：每个概念组都必须命中。
- `or`：至少一个概念组命中。
- 别名是同一概念组的备选表达，例如 `(JS OR JavaScript) AND (后端 OR 服务端)`。

### 3.2 向量模式

- `operator` 必须是 `smart`。
- 用户可开启/关闭 LLM 语义改写，默认关闭。
- 改写成功时，改写文本用于 embedding 和 reranker；失败时两者都使用原查询。

### 3.3 混合模式

- FTS 永远使用确定性词典生成的词法查询。
- 用户开启改写时，只有 embedding 和 reranker 使用语义改写文本。
- FTS 和语义分支并行运行；LLM 超时不得阻塞 FTS 返回。

## 4. 词法模型

新建 `search/lexicon.py`，统一定义：

```python
@dataclass(frozen=True, slots=True)
class LexicalConcept:
    canonical: str
    aliases: tuple[str, ...]

LEXICON_VERSION = "1"

def normalize_skill(value: str) -> str: ...
def tokenize_lexical_text(text: str) -> tuple[str, ...]: ...
def concepts_from_query(text: str) -> tuple[LexicalConcept, ...]: ...
def expand_document_tokens(values: Iterable[str]) -> tuple[str, ...]: ...
```

词法规则：

- 中文使用固定版本 `jieba` 分词，并加载项目内自定义技能词典。
- 英文执行 `casefold`，但保留 `C++`、`C#`、`.NET`、`Node.js` 等技术标记边界。
- 静态技能与学历映射由该模块供给文档侧和查询侧。
- 学校标准名和别名仍以 `SchoolReference` 为唯一数据源，不在词法模块内复制第二份学校表。`SchoolReference.alias_groups()` 一次返回标准名与别名组，文档构建与查询概念解析都注入同一组数据。

## 5. 索引文档

候选人文档分离为：

- `keyword_index_text`：空格分隔的规范词和别名，仅供 FTS 使用。
- `keyword_text`：保留可读文本，用于命中证据、排除校验与调试。
- `vector_text`：保留现有语义文本，技能只保留规范名，不重复注入别名。

LanceDB FTS 索引改为 `keyword_index_text + whitespace tokenizer`。物理 schema 改变时将 `INDEX_SCHEMA_VERSION` 和 `INDEX_CHUNK_VERSION` 同时提升到 `5`，旧索引必须显式重建。迁移工具只能在新旧 `vector_text` 字符串完全相同时复用已有向量；技能规范名导致该文本变化时必须重新 embedding。

## 6. API 与响应

`CandidateSearchRequest` 增加：

```python
operator: Literal["smart", "and", "or"] = "smart"
rewrite_enabled: bool = False
```

组合校验：

- `keyword + rewrite_enabled=true` 返回 422。
- `vector/hybrid + operator!=smart` 返回 422。
- 空查询可带精确筛选，但 `operator` 和改写均不参与执行。

`CandidateSearchResponse` 增加：

```python
query_plan: QueryPlanResponse

class QueryPlanResponse(BaseModel):
    operator: Literal["smart", "and", "or"]
    rewrite_requested: bool
    rewrite_status: Literal["disabled", "not_applicable", "success", "unavailable"]
    semantic_query: str | None
```

LLM 改写不可用属于可选增强失败，不写入 `degraded_reasons`，避免把有正常结果的搜索标成整体降级。

## 7. LLM 语义改写

新建 `search/rewrite.py`：

```python
class QueryRewriter(Protocol):
    async def rewrite(self, keywords: str) -> str: ...

class SemanticQueryRewriter:
    def __init__(self, client: OpenAICompatibleClient, *, cache_size: int = 256,
                 ttl_seconds: float = 600.0) -> None: ...
```

约束：

- 输入是 `parse_query` 完成硬条件剥离后的 `keywords`。
- 输出使用 JSON `{"semantic_query": "..."}`，非空且最长 500 字符。
- prompt 禁止增加城市、学历、年限、学校等原查询不存在的硬要求。
- 改写结果经 `parse_query` 检查；如果产生了新硬筛选，视为不可用并回退。
- 缓存键为 `模型识别 + LEXICON_VERSION + 规范化原关键词`，最多 256 条，TTL 600 秒。
- 单次改写子预算不超过 `min(1.0 秒, 剩余总预算的 25%)`。
- 改写超时、未配置 LLM、结构错误或输出越界时，继续使用原查询。

## 8. 前端交互

- 关键词模式显示“关键词逻辑”三态选择：智能排序/同时满足/满足任一。
- 向量和混合模式显示“AI 语义改写”开关，默认关闭，并告知“可能增加最多约 1 秒”。
- 两个选择分别写入 `localStorage`，但切换模式时只发送当前模式合法的参数。
- `rewrite_status=success` 可显示轻量“AI 已改写”状态；`unavailable` 显示“AI 改写不可用，已使用原搜索词”，不阻断结果。

## 9. 验收标准

### 9.1 确定性正确性

- `Java` 不得因子串误匹配只写 `JavaScript` 的候选人。
- `JS` 必须命中写 `JavaScript` 的候选人。
- `k8s` 必须命中写 `Kubernetes` 的候选人。
- `C++`、`C#`、`.NET`、`Node.js` 词边界不得被破坏。
- `北大` 与 `北京大学` 在学校精确筛选中结果一致。
- `Java AND 后端` 只返回同时命中两个概念组的候选人。
- `Java OR 后端` 返回任一概念组命中的候选人。

### 9.2 LLM 安全回退

- 改写关闭时不发起 LLM 请求。
- 关键词模式永远不发起 LLM 请求。
- LLM 超时、500、无效 JSON、空输出或新增硬要求时，返回结果与关闭改写的基础查询可用性一致。
- 混合模式中 LLM 超时时，FTS 结果仍在总 deadline 内返回。
- LLM 不得修改已解析的学历、城市、年限、学校等 filters。

### 9.3 质量与性能门禁

在固定黄金查询集上：

- 关键词 `Precision@20` 不低于基线。
- 别名查询 `Recall@20` 高于基线，且所有指定别名用例必须通过。
- 混合搜索 `nDCG@10` 不低于基线。
- 关键词模式 P95 不高于 500ms。
- 改写关闭时，向量/混合模式 P95 不高于当前基线的 110%。
- 改写开启时，整个搜索仍不超过现有 4.5 秒总 deadline。

### 9.4 发布门禁

- 后端全量 pytest 通过。
- 前端全量单元测试通过。
- 前端生产构建成功。
- Playwright E2E 通过，覆盖三种模式、AND/OR 和改写开关。
- PyInstaller sidecar 构建成功，断网环境可加载 jieba 词典并完成关键词搜索。
- 用 `1` 目录内测试简历完成一次真实 API 验收，并保存请求、响应摘要和耗时证据。
