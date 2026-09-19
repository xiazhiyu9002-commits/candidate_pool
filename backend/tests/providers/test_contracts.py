import pytest

from kerui_recruit.providers.fakes import FakeEmbeddingProvider, FakeRerankerProvider


@pytest.mark.asyncio
async def test_fake_embedding_is_stable_and_has_requested_dimension() -> None:
    """Changing fake embeddings between runs would make search tests flaky."""
    provider = FakeEmbeddingProvider(dimension=16)

    first = await provider.embed_documents(["Python 金融"])
    second = await provider.embed_documents(["Python 金融"])

    assert first == second
    assert len(first[0]) == 16
    assert first[0] != (await provider.embed_documents(["Java 制造"]))[0]


@pytest.mark.asyncio
async def test_fake_query_embedding_uses_the_same_vector_space() -> None:
    """Query and document embeddings for identical text must be directly comparable."""
    provider = FakeEmbeddingProvider(dimension=16)

    document = (await provider.embed_documents(["Python 金融"]))[0]

    assert await provider.embed_query("Python 金融") == document


@pytest.mark.asyncio
async def test_fake_reranker_orders_by_literal_query_evidence() -> None:
    """A fake reranker must reward visible evidence instead of fixed fixture order."""
    provider = FakeRerankerProvider()

    ranked = await provider.rerank(
        "Python 金融",
        ["平面设计", "Python 后端", "Python 金融风控"],
    )

    assert ranked == [2, 1, 0]
