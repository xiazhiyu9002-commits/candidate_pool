import asyncio
import json
import time
from dataclasses import replace

import pytest

from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchRequest
from kerui_recruit.search.lancedb_index import (
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)
from kerui_recruit.search.observer import InMemorySearchObserver
from kerui_recruit.search.service import HybridSearchService


def chunk(cid, number=0, content="Python", **kwargs):
    return SearchChunk(f"{cid}-{number}", cid, f"r-{cid}", content, (1., 0.), 5,
                       "MASTER", "上海", "AVAILABLE", **kwargs)


class Embedding:
    async def embed_query(self, text):
        return [1., 0.]


class SlowEmbedding:
    async def embed_query(self, text):
        await asyncio.sleep(5)


def service(index, timeout=2, embedding=None):
    return HybridSearchService(index=index, embedding_provider=embedding or Embedding(),
                               reranker_provider=FakeRerankerProvider(), search_timeout=timeout)


def test_complete_evidence_reads_more_than_default_ten_rows(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a", i, "Java" if i == 25 else "Python") for i in range(26)])
    assert len(index.get_candidate_chunk_contents(["a"])["a"]) == 26


@pytest.mark.asyncio
async def test_chunk_heavy_candidate_does_not_consume_candidate_limit(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a", i) for i in range(420)] + [chunk("b")])
    page = await service(index).search("", CandidateFilters(), limit=2)
    assert {hit.candidate_id for hit in page.items} == {"a", "b"}
    assert {row["candidate_id"] for row in index.search_vector((1., 0.), CandidateFilters(), 2)} == {"a", "b"}


@pytest.mark.asyncio
async def test_observer_none_matches_results_and_injected_observer_collects_phases(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a", 0, "Java 后端"), chunk("b", 0, "Python 算法")])
    svc = service(index)
    baseline = await svc.search("Java", CandidateFilters(), limit=5)
    observer = InMemorySearchObserver()
    observed = await svc.search("Java", CandidateFilters(), limit=5, observer=observer)
    # observer=None 与注入 observer 的结果完全一致。
    assert [h.candidate_id for h in baseline.items] == [h.candidate_id for h in observed.items]
    # 注入 observer 后才采集阶段耗时，且覆盖正式代码路径的单调时钟分段。
    assert observer.events > 0
    assert {"fts", "embedding", "vector", "fusion", "rerank"} <= set(observer.phases)


def test_all_preferred_locations_are_searchable_without_matching_current_city(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    assert "preferred_locations" in SearchChunk.__dataclass_fields__
    index.upsert([chunk("a", preferred_locations=("深圳", "广州")),
                  chunk("b", preferred_locations=("北京",))])
    hits = index.filter_search(CandidateFilters(preferred_locations=("广州",)), 10)
    assert [hit.candidate_id for hit in hits] == ["a"]


def test_multi_value_direction_filters_use_or_within_and_across_fields(tmp_path):
    """同一字段多值为 OR，跨字段为 AND——「多层筛选」必须能组合出交集。"""
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([
        chunk("a", career_directions=("BACKEND",), career_specializations=("BACKEND_SERVICE",),
              business_directions=("INSURANCE",)),
        chunk("b", career_directions=("DATA",), career_specializations=("DATA_WAREHOUSE",),
              business_directions=("INSURANCE",)),
        chunk("c", career_directions=("DATA",), career_specializations=("DATA_ANALYSIS",),
              business_directions=("GAMING",)),
        chunk("d"),
    ])

    # 单字段多值：OR
    assert {h.candidate_id for h in index.filter_search(
        CandidateFilters(career_directions=("BACKEND", "DATA")), 10)} == {"a", "b", "c"}
    # 跨字段：AND
    assert {h.candidate_id for h in index.filter_search(
        CandidateFilters(career_directions=("DATA",), business_directions=("INSURANCE",)), 10)} == {"b"}
    # 大类 + 细分组合
    assert {h.candidate_id for h in index.filter_search(
        CandidateFilters(career_directions=("BACKEND",), career_specializations=("BACKEND_SERVICE",)), 10)} == {"a"}
    # 无标签的候选人不会被误命中
    assert index.filter_search(CandidateFilters(business_directions=("MARKETING",)), 10) == []


def test_missing_multi_value_columns_relax_recall_instead_of_crashing(tmp_path):
    """旧索引物理缺列时，方向筛选必须放宽召回而不是报错（只读回退路径）。"""
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a"), chunk("b")])
    index.database.open_table(index.table_name).drop_columns(
        ["career_directions", "career_specializations", "business_directions"]
    )
    assert index._array_any_clause("career_directions", ("BACKEND",)) is None
    # 缺列 → 子句被跳过：不报错，也不会把候选人全部过滤掉
    assert {h.candidate_id for h in index.filter_search(
        CandidateFilters(career_directions=("BACKEND",)), 10)} == {"a", "b"}


def test_array_any_clause_skips_empty_values_and_quotes_apostrophes(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    assert index._array_any_clause("career_directions", ()) is None
    assert index._array_any_clause("school_tags", ("985",)) == "array_has_any(school_tags, ['985'])"
    assert index._array_any_clause("career_directions", ("O'Brien",)) == \
        "array_has_any(career_directions, ['O''Brien'])"


@pytest.mark.asyncio
async def test_missing_complete_evidence_fails_closed(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    index.get_candidate_chunk_contents = lambda ids: {}
    page = await service(index).search("", CandidateFilters(exclude_skills=("Java",)), limit=2)
    assert page.items == ()
    assert "EXCLUSION_UNVERIFIED" in page.degraded_reasons
    assert page.empty_reason == "service_error"


@pytest.mark.asyncio
async def test_evidence_read_obeys_same_deadline(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    def slow_evidence(ids):
        time.sleep(.35)
        return {"a": ["Python"]}
    index.get_candidate_chunk_contents = slow_evidence
    started = time.monotonic()
    page = await service(index, .06).search("", CandidateFilters(exclude_skills=("Java",)), limit=2)
    assert time.monotonic() - started < .22
    assert page.items == ()
    assert "EXCLUSION_UNVERIFIED" in page.degraded_reasons


@pytest.mark.asyncio
async def test_slow_fts_gets_no_extra_budget_after_embedding_timeout(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    original = index.search_fts
    def slow_fts(*args):
        time.sleep(.35)
        return original(*args)
    index.search_fts = slow_fts
    started = time.monotonic()
    page = await service(index, .06, SlowEmbedding()).search("Python", CandidateFilters(), limit=2)
    assert time.monotonic() - started < .22
    assert page.items == ()


def test_model_metadata_blocks_mixing_and_preserves_existing_rows(tmp_path):
    import inspect
    assert "embedding_model" in inspect.signature(LanceDBSearchIndex).parameters
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2, embedding_model="model-a")
    index.upsert([chunk("a")])
    incompatible = LanceDBSearchIndex(tmp_path, vector_dimension=2, embedding_model="model-b")
    assert not incompatible.is_ready()
    with pytest.raises(ValueError, match="incompatible"):
        incompatible.upsert([chunk("b")])
    assert index.warmup() == 1


def test_replace_candidate_removes_old_revision_and_projection_updates_preserve_vector(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a"), chunk("b")])
    assert hasattr(index, "replace_candidate")
    index.replace_candidate([replace(chunk("a", 1), revision_id="new")])
    assert index.get_revision_chunks("r-a") == []
    index.update_candidate_filters("a", candidate_status="ON_HOLD")
    assert [h.candidate_id for h in index.filter_search(CandidateFilters(), 10)] == ["b"]
    assert list(index.get_revision_chunks("new")[0]["vector"]) == [1., 0.]
    index.delete_candidate("a")
    assert index.get_revision_chunks("new") == []


@pytest.mark.asyncio
async def test_verified_fts_exclusions_survive_embedding_timeout(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a"), chunk("b", content="Java Python")])
    # 预算要留足 FTS + 排除复核的余量：本用例断言的是“向量通道超时后 FTS 结果仍在”，
    # 预算过紧（曾为 .15s）会被冷启动打开表的开销吃掉，退化成“FTS 也没跑完”的 TIMEOUT。
    page = await service(index, .6, SlowEmbedding()).search("Python", CandidateFilters(exclude_skills=("Java",)), limit=2)
    assert [hit.candidate_id for hit in page.items] == ["a"]
    assert "EMBEDDING_UNAVAILABLE" in page.degraded_reasons


@pytest.mark.asyncio
async def test_blocking_completion_releases_captured_semaphore_not_global():
    """超时/后台完成的原生调用只释放“当时获取”的信号量实例。

    测试隔离会调用 reset_search_pool() 替换全局 _SEARCH_SLOTS；若完成回调仍引用
    全局变量，会把 permit 释放到从未获取的新信号量上，触发 Semaphore released
    too many times。这里用可控 Future 确定性复现并断言释放到正确实例。
    """
    import concurrent.futures
    import threading

    from kerui_recruit.search import service as search_service

    controlled = concurrent.futures.Future()

    class TrackingSemaphore:
        def __init__(self):
            self._sem = threading.Semaphore(8)
            self.released = 0

        def acquire(self, blocking=True, timeout=None):
            return self._sem.acquire(blocking, timeout)

        def release(self):
            self.released += 1
            return self._sem.release()

    first = TrackingSemaphore()
    second = TrackingSemaphore()

    class Pool:
        def submit(self, fn, *args, **kwargs):
            return controlled

    old_pool, old_slots = search_service._SEARCH_POOL, search_service._SEARCH_SLOTS
    search_service._SEARCH_POOL = Pool()
    search_service._SEARCH_SLOTS = first
    try:
        task = asyncio.create_task(
            search_service._blocking(lambda: "done", deadline=time.monotonic() + 5.0)
        )
        await asyncio.sleep(0)
        # 模拟测试隔离：替换全局信号量。
        search_service._SEARCH_SLOTS = second
        controlled.set_result("done")
        await task
        # 完成回调必须释放“捕获”的 first，而非全局的 second。
        assert first.released == 1
        assert second.released == 0
    finally:
        search_service._SEARCH_POOL, search_service._SEARCH_SLOTS = old_pool, old_slots


@pytest.mark.asyncio
async def test_cancelled_outer_search_cancels_provider_child(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    finished = asyncio.Event()
    class EmbeddingChild:
        async def embed_query(self, text):
            try:
                await asyncio.sleep(5)
            finally:
                finished.set()
    await service(index, .4, EmbeddingChild()).search("Python", CandidateFilters(), limit=2)
    await asyncio.sleep(.02)
    assert finished.is_set()


def test_old_metadata_missing_index_is_not_migrated_or_deleted(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    (tmp_path / "candidate-index-metadata.json").unlink()
    assert not index.is_ready()
    with pytest.raises(ValueError, match="incompatible"):
        index.upsert([chunk("b")])
    assert index.database.open_table(index.table_name).count_rows() == 1


@pytest.mark.parametrize("changed", [{"vector_dimension": 3}, {"schema_version": "11"}, {"chunk_version": "2"}])
def test_all_index_contract_changes_require_explicit_rebuild(tmp_path, changed):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    settings = {"vector_dimension": 2, **changed}
    reopened = LanceDBSearchIndex(tmp_path, **settings)
    # 任何契约变化（写入侧）都要求显式重建，写入被阻断。
    assert not reopened.is_compatible()
    # 仅向量维度/模型变化会破坏「可读」；schema/chunk 版本变化（如旧 8/6）仍可只读回退。
    if "vector_dimension" in changed:
        assert not reopened.is_ready()
    else:
        assert reopened.is_ready()


def test_legacy_schema_metadata_stays_readable_and_searchable(tmp_path):
    """旧索引（schema 8 / chunk 6）在新代码下仍可只读检索，写入被阻断。"""
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a"), chunk("b")])
    (tmp_path / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "8", "embedding_model": "unspecified",
                    "vector_dimension": 2, "chunk_version": "6"}), encoding="utf-8")

    legacy = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    # 可读：is_ready 为 True；不可写：is_compatible 为 False。
    assert legacy.is_ready()
    assert not legacy.is_compatible()
    assert legacy.read_compatibility_error is None
    # 关键词检索仍可用。
    assert {row["candidate_id"] for row in legacy.search_fts("Python", CandidateFilters(), 10)} == {"a", "b"}
    # 向量检索仍可用。
    assert {row["candidate_id"] for row in legacy.search_vector((1.0, 0.0), CandidateFilters(), 10)} == {"a", "b"}
    # 混合检索仍可用。
    hits = legacy.search(SearchRequest(query="Python", query_vector=(1.0, 0.0), filters=CandidateFilters(), limit=10))
    assert {hit.candidate_id for hit in hits} == {"a", "b"}
    # 写入被阻断。
    with pytest.raises(ValueError, match="incompatible"):
        legacy.upsert([chunk("c")])


def test_upgrade_legacy_schema_makes_index_writable(tmp_path):
    """旧 schema 8 索引经显式升级后（补列 + 更新 metadata）可写入新数据。"""
    import lancedb as ldb
    import pyarrow as pa

    probe = LanceDBSearchIndex(tmp_path / "probe", vector_dimension=2)
    full = probe._schema()
    drop = {"kind", "sequence", "evidence_path", "specializations"}
    schema8 = pa.schema([f for f in full if f.name not in drop])

    root = tmp_path / "legacy"
    root.mkdir()
    db = ldb.connect(str(root))
    db.create_table("candidate_chunks", schema=schema8)
    (root / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "8", "embedding_model": "unspecified",
                    "vector_dimension": 2, "chunk_version": "6"}), encoding="utf-8")

    index = LanceDBSearchIndex(root, vector_dimension=2)
    assert not index.is_compatible()
    assert index.is_readable()
    assert index.upgrade_legacy_schema() is True
    assert index.is_compatible()
    # 迁移后可写入新数据。
    index.upsert([chunk("a")])
    assert index.warmup() == 1


def test_upgrade_legacy_schema_rejects_non_legacy_version(tmp_path):
    """升级仅接受已知旧版本 schema 8 / chunk 6，其它版本一律拒绝。"""
    import lancedb as ldb
    import pyarrow as pa

    probe = LanceDBSearchIndex(tmp_path / "probe", vector_dimension=2)
    full = probe._schema()
    drop = {"kind", "sequence", "evidence_path", "specializations"}
    schema8 = pa.schema([f for f in full if f.name not in drop])

    root = tmp_path / "legacy"
    root.mkdir()
    db = ldb.connect(str(root))
    db.create_table("candidate_chunks", schema=schema8)
    (root / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "7", "embedding_model": "unspecified",
                    "vector_dimension": 2, "chunk_version": "5"}), encoding="utf-8")

    index = LanceDBSearchIndex(root, vector_dimension=2)
    with pytest.raises(ValueError, match="only schema 8 / chunk 6"):
        index.upgrade_legacy_schema()


@pytest.mark.asyncio
async def test_provider_ignoring_cancellation_cannot_accumulate_without_bound(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    release = asyncio.Event()
    active = 0
    peak = 0
    class StubbornEmbedding:
        async def embed_query(self, text):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            finally:
                active -= 1
            return [1., 0.]
    search = service(index, .15, StubbornEmbedding())
    try:
        for _ in range(12):
            await search.search("Python", CandidateFilters(), limit=2)
        assert peak <= 4  # 上游并发上限（_PROVIDER_CONCURRENCY）
    finally:
        release.set()
        await asyncio.sleep(.02)


def test_preferred_location_projection_update_replaces_old_choices(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a", preferred_location="北京", preferred_locations=("北京", "上海"))])
    index.update_candidate_filters("a", preferred_location="广州")
    assert [hit.candidate_id for hit in index.filter_search(CandidateFilters(preferred_locations=("广州",)), 10)] == ["a"]
    assert index.filter_search(CandidateFilters(preferred_locations=("北京", "上海")), 10) == []


@pytest.mark.asyncio
async def test_timed_out_candidate_scan_does_not_start_more_native_queries(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a", i) for i in range(420)] + [chunk("b")])
    original = index._candidate_rows
    calls = 0
    class SlowBuilder:
        def __init__(self, builder):
            self.builder = builder
        def limit(self, limit):
            self.builder = self.builder.limit(limit)
            return self
        def to_list(self):
            nonlocal calls
            calls += 1
            time.sleep(.6)
            return self.builder.to_list()
    index._candidate_rows = lambda builder, count, limit, filters: original(SlowBuilder(builder), count, limit, filters)
    # 预算必须覆盖 is_ready + 打开表 + count_rows 的前置开销（实测可达 60ms+），否则
    # 第一次 _check_deadline() 就抛超时、calls 停在 0，用例就不再检验它要检验的不变量。
    page = await service(index, .25).search("", CandidateFilters(), limit=2)
    assert not page.items
    await asyncio.sleep(.8)
    assert calls == 1  # Existing native call can finish, but later scan pages must not start.


def test_word_level_fts_boundaries(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([
        chunk("java", content="Java", keyword_index_text="java"),
        chunk("javascript", content="JavaScript", keyword_index_text="javascript"),
        chunk("cpp", content="C++", keyword_index_text="c++"),
        chunk("csharp", content="C#", keyword_index_text="c#"),
        chunk("dotnet", content=".NET", keyword_index_text=".net"),
        chunk("node", content="Node.js", keyword_index_text="node.js"),
    ])
    assert {row["candidate_id"] for row in index.search_fts("java", CandidateFilters(), 20)} == {"java"}
    assert {row["candidate_id"] for row in index.search_fts("c++", CandidateFilters(), 20)} == {"cpp"}
    assert {row["candidate_id"] for row in index.search_fts("c#", CandidateFilters(), 20)} == {"csharp"}
    assert {row["candidate_id"] for row in index.search_fts(".net", CandidateFilters(), 20)} == {"dotnet"}
    assert {row["candidate_id"] for row in index.search_fts("node.js", CandidateFilters(), 20)} == {"node"}


def test_index_version_is_ten():
    assert INDEX_SCHEMA_VERSION == "10"
    assert INDEX_CHUNK_VERSION == "10"


def test_version_four_metadata_requires_rebuild(tmp_path):
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    (tmp_path / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "4", "embedding_model": "unspecified",
                    "vector_dimension": 2, "chunk_version": "4"}), encoding="utf-8")
    assert not index.is_compatible()
    assert "rebuild" in (index.compatibility_error or "").lower()


def test_v8_chunk_index_requires_rebuild_but_stays_readable(tmp_path):
    """chunk 8（v8 文档口径）必须被判为需要重建，但只读检索仍可回退使用。"""
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    metadata_path = tmp_path / "candidate-index-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["chunk_version"] = "8"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    stale = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    assert not stale.is_compatible()
    assert "explicit rebuild" in (stale.compatibility_error or "")
    assert stale.is_readable()
    assert stale.is_ready()


def test_current_chunk_version_rebuild_is_compatible_and_writable(tmp_path):
    """按当前 chunk 口径重建后兼容性通过、可写。"""
    index = LanceDBSearchIndex(tmp_path, vector_dimension=2)
    index.upsert([chunk("a")])
    metadata = json.loads((tmp_path / "candidate-index-metadata.json").read_text(encoding="utf-8"))
    assert metadata["chunk_version"] == "10"
    assert index.is_compatible()
    index.upsert([chunk("b")])
    assert {row["candidate_id"] for row in index.search_fts("Python", CandidateFilters(), 20)} == {"a", "b"}
