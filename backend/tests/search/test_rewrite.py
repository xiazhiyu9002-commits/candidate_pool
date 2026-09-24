import pytest

from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.search import rewrite as rewrite_module
from kerui_recruit.search.rewrite import SemanticQueryRewriter


class FakeClient:
    """模拟 OpenAICompatibleClient：可配置返回结构化结果或抛错。"""

    def __init__(self, semantic_query=None, error=None, model="fake-model"):
        self.model = model
        self.semantic_query = semantic_query
        self.error = error
        self.calls = 0
        self.messages: list[list[dict]] = []

    async def complete_json(self, messages, response_model, temperature=None, *, execution_context=None, deadline_monotonic=None):
        self.calls += 1
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return response_model(semantic_query=self.semantic_query)


@pytest.mark.asyncio
async def test_rewrite_success_aligns_synonyms():
    """保真规范化示例：只把别名对齐到标准名，不新增概念。"""
    client = FakeClient(semantic_query="JavaScript 后端开发")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 后端")
    assert result.query == "JavaScript 后端开发"
    assert result.outcome == "changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("original,rewritten", [
    ("JS 后端", "JavaScript 后端开发"),
    ("K8s 微服务", "Kubernetes 微服务"),
    ("数仓 TL", "数据仓库 技术负责人 TL"),
])
async def test_rewrite_normalises_known_abbreviations(original, rewritten):
    client = FakeClient(semantic_query=rewritten)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite(original)
    assert result.query == rewritten
    assert result.outcome == "changed"


@pytest.mark.asyncio
async def test_rewrite_added_duty_concept_is_rejected():
    """“JS 交易系统”不得被改写成“JavaScript 交易系统服务端开发”（凭空新增后端概念）。"""
    client = FakeClient(semantic_query="JavaScript 交易系统服务端开发")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_prompt_is_faithful_normalizer():
    """提示词必须是保真规范化器，明确禁止联想扩写与新增条件。"""
    client = FakeClient(semantic_query="JavaScript 交易系统")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS 交易系统")
    system = client.messages[0][0]["content"]
    assert "保真规范化器" in system
    assert "不是联想扩写器" in system
    assert "禁止根据岗位名称推断并新增技能" in system


@pytest.mark.asyncio
async def test_rewrite_same_text_is_unchanged():
    client = FakeClient(semantic_query="核心交易系统 开发")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("核心交易系统 开发")
    assert result.query == "核心交易系统 开发"
    assert result.outcome == "unchanged"


@pytest.mark.asyncio
async def test_rewrite_empty_output_is_rejected():
    client = FakeClient(semantic_query="")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_overlong_output_is_rejected():
    client = FakeClient(semantic_query="x" * 501)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_invalid_json_is_unavailable():
    client = FakeClient(error=ProviderError("E_API_SCHEMA", True, "bad json"))
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Java 上海", "Java 硕士"])
async def test_rewrite_new_hard_filter_is_rejected(bad):
    client = FakeClient(semantic_query=bad)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["JavaScript or Java", "JavaScript（同义词）", "技能术语：JavaScript"])
async def test_rewrite_list_marker_is_rejected(bad):
    client = FakeClient(semantic_query=bad)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_added_years_condition_is_rejected():
    """长度、列表标记、概念保真都通过时，新增年限条件仍必须被硬条件校验拦下。"""
    client = FakeClient(semantic_query="JavaScript 后端 5年以上")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 后端")
    assert result.query == "JS 后端"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_new_curated_skill_is_rejected():
    """“软件工程师 后端”不得凭空新增微服务、Redis 等已策展技能概念。"""
    client = FakeClient(semantic_query="软件工程师 后端 微服务 Redis")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("软件工程师 后端")
    assert result.query == "软件工程师 后端"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_dropped_curated_concept_is_rejected():
    """原查询的已策展概念必须全部保留。"""
    client = FakeClient(semantic_query="JavaScript 后端")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS Kafka 后端")
    assert result.query == "JS Kafka 后端"
    assert result.outcome == "rejected"


@pytest.mark.asyncio
async def test_rewrite_caches_identical_calls():
    client = FakeClient(semantic_query="JavaScript 交易系统服务端开发")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS 交易系统")
    await rewriter.rewrite("JS 交易系统")
    assert client.calls == 1


@pytest.mark.asyncio
async def test_rewrite_cache_misses_on_different_source_text():
    client = FakeClient(semantic_query="JavaScript")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("Java")
    await rewriter.rewrite("Python")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rewrite_cache_misses_on_model_change():
    client = FakeClient(semantic_query="JavaScript", model="model-a")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS")
    client.model = "model-b"
    await rewriter.rewrite("JS")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rewrite_cache_misses_on_lexicon_version_change(monkeypatch):
    client = FakeClient(semantic_query="JavaScript")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS")
    monkeypatch.setattr(rewrite_module, "LEXICON_VERSION", "2-changed")
    await rewriter.rewrite("JS")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rewrite_cache_misses_on_prompt_version_change(monkeypatch):
    """提示词版本必须独立参与缓存键，升级后不能继续命中旧缓存。"""
    client = FakeClient(semantic_query="JavaScript")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS")
    monkeypatch.setattr(rewrite_module, "REWRITE_PROMPT_VERSION", "3")
    await rewriter.rewrite("JS")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rewrite_cache_misses_on_cache_identity_change():
    client = FakeClient(semantic_query="JavaScript")
    rewriter = SemanticQueryRewriter(client)
    await rewriter.rewrite("JS")
    client.cache_identity = "revision-2"
    await rewriter.rewrite("JS")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rewrite_cache_evicts_lru_on_257th_entry():
    client = FakeClient(semantic_query="JavaScript")
    rewriter = SemanticQueryRewriter(client, cache_size=256)
    for i in range(257):
        await rewriter.rewrite(f"skill{i}")
    assert client.calls == 257
    # 第 1 条（LRU）应已被逐出，重新调用会再次请求客户端。
    await rewriter.rewrite("skill0")
    assert client.calls == 258
