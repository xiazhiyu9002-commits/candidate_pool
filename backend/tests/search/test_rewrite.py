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

    async def complete_json(self, messages, response_model, temperature=None, *, execution_context=None, deadline_monotonic=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return response_model(semantic_query=self.semantic_query)


@pytest.mark.asyncio
async def test_rewrite_success_aligns_synonyms():
    client = FakeClient(semantic_query="JavaScript 交易系统服务端开发")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JavaScript 交易系统服务端开发"
    assert result.status == "success"


@pytest.mark.asyncio
async def test_rewrite_empty_output_is_unavailable():
    client = FakeClient(semantic_query="")
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_rewrite_overlong_output_is_unavailable():
    client = FakeClient(semantic_query="x" * 501)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_rewrite_invalid_json_is_unavailable():
    client = FakeClient(error=ProviderError("E_API_SCHEMA", True, "bad json"))
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Java 上海", "Java 硕士"])
async def test_rewrite_new_hard_filter_is_unavailable(bad):
    client = FakeClient(semantic_query=bad)
    rewriter = SemanticQueryRewriter(client)
    result = await rewriter.rewrite("JS 交易系统")
    assert result.query == "JS 交易系统"
    assert result.status == "unavailable"


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
