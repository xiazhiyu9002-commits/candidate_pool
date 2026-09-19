from __future__ import annotations

import asyncio
import logging
import time

from kerui_recruit.providers.contracts import EmbeddingProvider, RerankerProvider
from kerui_recruit.search.contracts import (
    CandidateFilters,
    QueryPlan,
    SearchPage,
    SearchRequest,
)
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex, SEARCH_DEADLINE
from kerui_recruit.search.lexicon import LexicalConcept, tokenize_lexical_text
from kerui_recruit.search.observer import SearchObserver
from kerui_recruit.search.query import has_skill

logger = logging.getLogger(__name__)


# 召回层阈值。
KEYWORD_MIN_HIT_TERMS = 1  # 关键词 smart 模式：至少命中的查询词数
# 向量绝对下限（比较对象是 score = 1/(1+L2²)；索引里 _distance 是 L2 距离的平方，归一化
# 向量下 score = 1/(3-2cos)，故 0.5263 ⇔ cos 0.55、0.6 ⇔ cos 0.667）。
#
# 两种模式的最优水位不同，实测（99 条真实 JD 查询 + 弱监督标签）：
# - 纯向量模式的向量通道就是终点，召回越全越好：下限从 cos 0.667 降到 0.55 后，
#   空结果 11→0、R@20 +82%（0.0320→0.0581）、R@100 +103%（0.0951→0.1933，p=0.000），
#   且 P@5 与 NDCG 不降——多召回的行都排在头部之后，没有精度代价。
# - 混合模式的向量通道只给 RRF 供料，而 RRF 只取 rank 不看分数：放低下限会让低相似度
#   行挤占「送入 reranker 的 top-100」名额，实测 NDCG@10 反而降 0.028（0.4261→0.3966，
#   逐查询 23 胜 42 负 p=0.025，同期 keyword 对照组 1 胜 1 负说明不是噪声）。
VECTOR_MIN_SIMILARITY = 0.5263  # 纯向量模式的绝对下限 ⇔ cos 0.55
VECTOR_FUSION_MIN_SIMILARITY = 0.6  # 混合模式向量通道的绝对下限 ⇔ cos 0.667
VECTOR_RELATIVE_RATIO = 0.9  # 向量相似度相对阈值：保留 >= top1 分数 × 0.9，随召回规模自适应

# 上游 provider 调用预算：并发上限 + TPM pacing（令牌桶）。超限排队到 deadline，
# 而不是立即降级——单次 429 造成的静默降级远贵于排队等待。
_PROVIDER_CONCURRENCY = 4  # 上游并发上限（原硬编码 8）
_PROVIDER_REQUESTS_PER_MINUTE = 48  # 稳态 48 req/min
_PROVIDER_MIN_INTERVAL = 60.0 / _PROVIDER_REQUESTS_PER_MINUTE  # 1.25s
_PROVIDER_PACING_BURST = float(_PROVIDER_CONCURRENCY)  # 突发额度：单次检索不产生额外等待

# 召回池上限：常规分页压到 120（仍 >= 融合封顶与重排窗口 100），深翻页/基准保持原深度。
POOL_LIMIT_MULTIPLIER = 3
POOL_LIMIT_FLOOR = 100
POOL_LIMIT_CAP = 120
POOL_LIMIT_CEILING = 5000


def _pool_limit(limit: int) -> int:
    """召回池上限：常规分页压到 120，深翻页/基准保持原深度。"""
    if limit > POOL_LIMIT_CAP:
        return min(limit * POOL_LIMIT_MULTIPLIER, POOL_LIMIT_CEILING)
    return min(max(limit * POOL_LIMIT_MULTIPLIER, POOL_LIMIT_FLOOR), POOL_LIMIT_CAP)


def _record_degraded(degraded: list[str], reason: str, exc: BaseException | None = None) -> None:
    """记录降级原因；provider 异常的分类与上游 request id 一并落日志。

    对外降级原因码保持不变（desktop 文案与既有断言依赖），分类与 request id 只进日志。
    """
    degraded.append(reason)
    if exc is None:
        logger.warning("search degraded reason=%s", reason)
        return
    logger.warning(
        "search degraded reason=%s error=%s code=%s category=%s request_id=%s retryable=%s",
        reason,
        type(exc).__name__,
        getattr(exc, "code", None),
        getattr(exc, "category", None),
        getattr(exc, "request_id", None),
        getattr(exc, "retryable", None),
    )


# Threads cannot be forcibly stopped by cancelling an asyncio waiter. Keep a
# shared, bounded pool and retain each permit until the actual native call ends.
from concurrent.futures import ThreadPoolExecutor
import threading

_SEARCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kerui-search")
_SEARCH_SLOTS = threading.BoundedSemaphore(8)


def reset_search_pool() -> None:
    """测试隔离：重置共享检索线程池与信号量。

    超时放弃的原生调用会在其线程完成前继续占用 worker 与槽位。仅替换信号量并不够：
    worker 仍被上一个用例的原生调用占着，新任务只会排在执行队列里，在超时前拿不到
    worker —— 表现为 is_ready/FTS 直接 TIMEOUT。这里同时替换线程池，恢复「8 个槽位
    对应 8 个可用 worker」的对应关系。生产环境不调用本函数，该对应关系由
    _blocking 的许可生命周期天然维持。
    """
    global _SEARCH_POOL, _SEARCH_SLOTS
    _SEARCH_POOL.shutdown(wait=False)
    _SEARCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kerui-search")
    _SEARCH_SLOTS = threading.BoundedSemaphore(8)


async def _until(task, deadline):
    if task.done():
        return task.result()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        task.cancel()
        raise TimeoutError()
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    raise TimeoutError()


async def _blocking(function, *args, deadline):
    # 捕获本次调用对应的信号量实例：完成回调只释放“当时获取”的实例，
    # 避免 reset_search_pool() 替换全局 _SEARCH_SLOTS 后回调释放错误的信号量，
    # 触发 Semaphore released too many times。
    slots = _SEARCH_SLOTS
    while not slots.acquire(blocking=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        await asyncio.sleep(min(.005, remaining))
    if time.monotonic() >= deadline:
        slots.release()
        raise TimeoutError()
    try:
        def invoke():
            token = SEARCH_DEADLINE.set(deadline)
            try:
                return function(*args)
            finally:
                SEARCH_DEADLINE.reset(token)
        future = _SEARCH_POOL.submit(invoke)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return await _until(asyncio.wrap_future(future), deadline)


class HybridSearchService:
    """One deadline covers readiness, retrieval, full evidence and reranking."""

    def __init__(self, *, index: LanceDBSearchIndex,
                 embedding_provider: EmbeddingProvider, reranker_provider: RerankerProvider,
                 search_timeout: float = 12.0, rewriter=None) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self.reranker_provider = reranker_provider
        self.search_timeout = search_timeout
        self.rewriter = rewriter
        self._provider_tasks: set[asyncio.Task] = set()
        self._provider_slots = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
        self._pacing_tokens = _PROVIDER_PACING_BURST
        self._pacing_updated = time.monotonic()

    async def _provider(self, function, *args, deadline):
        """在并发上限内排队执行远程调用；超限排队到 deadline，而不是立即降级。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider concurrency budget exhausted")
        try:
            await asyncio.wait_for(self._provider_slots.acquire(), timeout=remaining)
        except TimeoutError:
            raise TimeoutError("Provider concurrency budget exhausted") from None
        try:
            await self._pace(deadline)
        except BaseException:
            self._provider_slots.release()
            raise
        task = asyncio.create_task(function(*args))
        self._provider_tasks.add(task)

        def completed(done):
            self._provider_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve errors even when a timed-out caller has left.
            # 与 _blocking 同一契约：许可由真实调用结束时释放。忽略取消的调用仍占用
            # 许可，因此并发上限同时也是“未完成调用”的累积上限。
            self._provider_slots.release()

        task.add_done_callback(completed)
        return await _until(task, deadline)

    async def _pace(self, deadline):
        """TPM pacing：稳态 48 req/min，突发额度用尽后按 1.25s 间隔放行。"""
        while True:
            now = time.monotonic()
            self._pacing_tokens = min(
                _PROVIDER_PACING_BURST,
                self._pacing_tokens + (now - self._pacing_updated) / _PROVIDER_MIN_INTERVAL,
            )
            self._pacing_updated = now
            if self._pacing_tokens >= 1.0:
                self._pacing_tokens -= 1.0
                return
            wait = (1.0 - self._pacing_tokens) * _PROVIDER_MIN_INTERVAL
            if now + wait >= deadline:
                raise TimeoutError("Provider pacing budget exhausted")
            await asyncio.sleep(wait)

    def warmup(self) -> int:
        return self.index.warmup()

    def optimize_pending(self) -> bool:
        return self.index.optimize_pending()

    def is_ready(self) -> bool:
        return self.index.is_ready()

    async def search(self, query: str, filters: CandidateFilters, *, limit: int,
                     mode: str = "hybrid",
                     deadline: float | None = None,
                     observer: SearchObserver | None = None,
                     vector_query: str | None = None,
                     operator: str = "smart",
                     concepts: tuple[LexicalConcept, ...] = (),
                     rewrite_enabled: bool = False,
                     search_body: bool = False) -> SearchPage:
        budget = min(deadline, time.monotonic() + self.search_timeout) if deadline is not None else time.monotonic() + self.search_timeout
        degraded: list[str] = []
        semantic_query_used = vector_query or query
        rewrite_status = "unavailable" if rewrite_enabled else "disabled"

        def _plan() -> QueryPlan:
            if mode == "keyword":
                status, semantic = "not_applicable", None
            elif not query.strip():
                status, semantic = "disabled", None
            elif not rewrite_enabled:
                status, semantic = "disabled", None
            else:
                status = rewrite_status
                semantic = semantic_query_used if rewrite_status == "success" else None
            return QueryPlan(operator=operator, rewrite_requested=rewrite_enabled,
                             rewrite_status=status, semantic_query=semantic)

        try:
            ready = await _blocking(self.index.is_ready, deadline=budget)
        except TimeoutError:
            _record_degraded(degraded, "TIMEOUT")
            return SearchPage(items=(), empty_reason="service_error", degraded_reasons=("TIMEOUT",), query_plan=_plan())
        except Exception as exc:
            _record_degraded(degraded, "SEARCH_UNAVAILABLE", exc)
            return SearchPage(items=(), empty_reason="service_error", degraded_reasons=("SEARCH_UNAVAILABLE",), query_plan=_plan())
        if not ready:
            return SearchPage(items=(), empty_reason="index_not_ready", query_plan=_plan())

        pool_limit = _pool_limit(limit)
        if not query.strip():
            try:
                hits = await _blocking(self.index.filter_search, filters, limit, deadline=budget)
            except Exception as exc:
                reason = "EXCLUSION_UNVERIFIED" if filters.exclude_skills else "SEARCH_UNAVAILABLE"
                _record_degraded(degraded, reason, exc)
                return SearchPage(items=(), empty_reason="service_error", degraded_reasons=(reason,), query_plan=_plan())
        elif mode == "keyword":
            hits = await self._keyword_retrieve(query, filters, pool_limit, budget, degraded, observer, operator=operator, concepts=concepts, search_body=search_body)
        elif mode == "vector":
            hits, semantic_query_used, rewrite_status = await self._vector_retrieve(
                vector_query or query, filters, pool_limit, budget, degraded, observer, rewrite_enabled=rewrite_enabled)
        elif hasattr(self.index, "search_fts") and hasattr(self.index, "fuse"):
            hits, semantic_query_used, rewrite_status = await self._parallel_retrieve(
                query, filters, pool_limit, budget, degraded, observer, vector_query, rewrite_enabled=rewrite_enabled)
        else:
            hits = await self._legacy_retrieve(query, filters, pool_limit, budget, degraded, observer)

        hits = await self._apply_exclusion(hits, filters.exclude_skills, budget, degraded)
        if not hits:
            return SearchPage(items=(), degraded_reasons=tuple(dict.fromkeys(degraded)),
                              empty_reason="service_error" if degraded else "no_match", query_plan=_plan())
        if query.strip():
            # 纯关键词模式不使用语义重排，避免改变词法排序。
            if mode != "keyword" and time.monotonic() < budget:
                try:
                    contents = [hit.vector_text or hit.content for hit in hits[:100]]
                    if observer is not None:
                        _t = time.monotonic()
                    if hasattr(self.reranker_provider, "rerank_scored"):
                        scored = await self._provider(self.reranker_provider.rerank_scored, semantic_query_used, contents, deadline=budget)
                        hits = _apply_rerank_scored(hits, scored)
                        # 把 rerank 分回写 score，使 API 返回的 score 与最终排序一致。
                        hits = [_promote_rerank_score(hit) for hit in hits]
                    else:
                        order = await self._provider(self.reranker_provider.rerank, semantic_query_used, contents, deadline=budget)
                        hits = _apply_rerank_order(hits, order)
                    if observer is not None:
                        observer.record_phase("rerank", (time.monotonic() - _t) * 1000)
                except Exception as exc:
                    _record_degraded(degraded, "RERANKER_UNAVAILABLE", exc)
            elif mode != "keyword":
                _record_degraded(degraded, "TIMEOUT")
        hits = _dedupe_candidates(hits, limit)
        return SearchPage(items=tuple(hits), degraded_reasons=tuple(dict.fromkeys(degraded)), query_plan=_plan())

    async def _maybe_rewrite(self, query: str, rewrite_enabled: bool, budget: float) -> tuple[str, str]:
        """在子预算内改写；失败/无改写器均回退原查询。返回 (语义查询, rewrite_status)。"""
        if not rewrite_enabled:
            return query, "disabled"
        if self.rewriter is None:
            return query, "unavailable"
        rewrite_deadline = min(budget, time.monotonic() + min(5.0, max(0.0, (budget - time.monotonic()) * 0.6)))
        try:
            result = await self._provider(self.rewriter.rewrite, query, deadline=rewrite_deadline)
            if getattr(result, "status", None) == "success" and getattr(result, "query", None):
                return result.query, "success"
        except Exception as exc:
            # 改写失败不影响对外原因码（rewrite_status 仍是 unavailable），但不能再无声吞掉。
            logger.warning(
                "search rewrite degraded error=%s code=%s category=%s request_id=%s",
                type(exc).__name__,
                getattr(exc, "code", None),
                getattr(exc, "category", None),
                getattr(exc, "request_id", None),
            )
        return query, "unavailable"

    async def _keyword_retrieve(self, query, filters, limit, budget, degraded, observer=None,
                                operator="smart", concepts=(), search_body=False):
        """仅关键词召回，不调用 embedding，不使用语义重排。"""
        if not hasattr(self.index, "search_fts"):
            _record_degraded(degraded, "FTS_UNAVAILABLE")
            return []
        if operator in ("and", "or") and concepts and hasattr(self.index, "search_fts_boolean"):
            try:
                rows = await _blocking(self.index.search_fts_boolean, query, concepts, operator,
                                       filters, limit, search_body, deadline=budget)
                return self.index.hits_from_rows(rows, "bm25")
            except Exception as exc:
                _record_degraded(degraded, "FTS_UNAVAILABLE", exc)
                return []
        try:
            rows = await _blocking(self.index.search_fts, query, filters, limit, search_body, deadline=budget)
            rows = self._filter_by_hit_terms(rows, query)
            return self.index.hits_from_rows(rows, "bm25")
        except Exception as exc:
            _record_degraded(degraded, "FTS_UNAVAILABLE", exc)
            return []

    @staticmethod
    def _filter_by_hit_terms(rows, query):
        """关键词 smart 模式：过滤命中查询词过少的弱结果（仅命中 1 个泛词）。"""
        terms = set(tokenize_lexical_text(query))
        if len(terms) <= KEYWORD_MIN_HIT_TERMS:
            return rows
        kept = []
        for row in rows:
            row_terms = {t.casefold() for t in (row.get("keyword_index_text") or "").split()}
            if len(terms & row_terms) >= KEYWORD_MIN_HIT_TERMS:
                kept.append(row)
        return kept

    async def _vector_retrieve(self, query, filters, limit, budget, degraded, observer=None, rewrite_enabled=False):
        """仅向量召回：改写后生成查询向量再走向量检索。返回 (hits, 语义查询, rewrite_status)。"""
        semantic_query, rewrite_status = await self._maybe_rewrite(query, rewrite_enabled, budget)
        try:
            vector = await self._provider(self.embedding_provider.embed_query, semantic_query, deadline=budget)
        except Exception as exc:
            _record_degraded(degraded, "EMBEDDING_UNAVAILABLE", exc)
            return [], semantic_query, rewrite_status
        try:
            rows = await _blocking(self.index.search_vector, tuple(vector), filters, limit, deadline=budget)
            hits = self.index.hits_from_rows(rows, "vector")
            hits = self._apply_vector_threshold(hits)
            return hits, semantic_query, rewrite_status
        except Exception as exc:
            _record_degraded(degraded, "VECTOR_UNAVAILABLE", exc)
            return [], semantic_query, rewrite_status

    @staticmethod
    def _apply_vector_threshold(hits: list) -> list:
        """向量召回相对阈值：保留 >= top1 分数 × VECTOR_RELATIVE_RATIO 的结果。

        以绝对下限 VECTOR_MIN_SIMILARITY 兜底，避免 top1 本身很低时阈值过低。
        """
        if not hits:
            return hits
        top_score = max(hit.score for hit in hits)
        threshold = max(VECTOR_MIN_SIMILARITY, top_score * VECTOR_RELATIVE_RATIO)
        return [hit for hit in hits if hit.score >= threshold]

    async def _parallel_retrieve(self, query, filters, limit, budget, degraded, observer=None, vector_query=None, rewrite_enabled=False):
        semantic_query = vector_query or query
        rewrite_status = "disabled" if not rewrite_enabled else "unavailable"

        async def run_fts():
            _t = time.monotonic()
            try:
                return await _blocking(self.index.search_fts, query, filters, max(limit, 100), deadline=budget)
            finally:
                if observer is not None:
                    observer.record_phase("fts", (time.monotonic() - _t) * 1000)

        async def semantic():
            nonlocal semantic_query, rewrite_status
            semantic_query, rewrite_status = await self._maybe_rewrite(vector_query or query, rewrite_enabled, budget)
            _t_embed = time.monotonic()
            try:
                vector = await self._provider(self.embedding_provider.embed_query, semantic_query, deadline=budget)
            except Exception as exc:
                _record_degraded(degraded, "EMBEDDING_UNAVAILABLE", exc)
                return []
            finally:
                if observer is not None:
                    observer.record_phase("embedding", (time.monotonic() - _t_embed) * 1000)
            _t_vector = time.monotonic()
            try:
                rows = await _blocking(self.index.search_vector, tuple(vector), filters, max(limit, 100), deadline=budget)
                # 混合通道向量召回加相对相似度阈值，过滤低相似度噪声（与纯向量模式一致）。
                scored = [r for r in rows if isinstance(r.get("_distance"), (int, float))]
                if scored:
                    top = max(1.0 / (1.0 + r["_distance"]) for r in scored)
                    # 混合通道的下限比纯向量模式更高：这里的作用只是给 RRF 供料，而 RRF 只取
                    # rank 不看分数，放低会让低相似度行挤占送入 reranker 的 top-100 名额。
                    threshold = max(VECTOR_FUSION_MIN_SIMILARITY, top * VECTOR_RELATIVE_RATIO)
                    rows = [r for r in scored if 1.0 / (1.0 + r["_distance"]) >= threshold]
                else:
                    rows = []
                return rows
            except Exception as exc:
                _record_degraded(degraded, "VECTOR_UNAVAILABLE", exc)
                return []
            finally:
                if observer is not None:
                    observer.record_phase("vector", (time.monotonic() - _t_vector) * 1000)

        fts = asyncio.create_task(run_fts())
        vector_task = asyncio.create_task(semantic())
        try:
            # 不在此处用 wall-clock timeout 强制取消：FTS 与 embedding 各自通过
            # 内部 deadline（_blocking/_provider 的 budget）控制超时。若用
            # asyncio.wait 的 timeout，会在 FTS 尚未完成时误取消它，导致
            # 「embedding 超时但 FTS 已成功」时丢失全文结果。
            await asyncio.wait({fts, vector_task})
            rows = []
            for task, reason in ((fts, "FTS_UNAVAILABLE"), (vector_task, "EMBEDDING_UNAVAILABLE")):
                if task.done() and not task.cancelled():
                    try:
                        rows.append(task.result())
                    except Exception as exc:
                        _record_degraded(degraded, reason, exc)
                        rows.append([])
                else:
                    task.cancel()
                    _record_degraded(degraded, reason)
                    rows.append([])
            _t_fuse = time.monotonic()
            fused = self.index.fuse(rows[0], rows[1], max(limit, 100))
            if observer is not None:
                observer.record_phase("fusion", (time.monotonic() - _t_fuse) * 1000)
            return fused, semantic_query, rewrite_status
        finally:
            for task in (fts, vector_task):
                if not task.done():
                    task.cancel()

    async def _legacy_retrieve(self, query, filters, limit, budget, degraded, observer=None):
        vector = ()
        try:
            vector = tuple(await self._provider(self.embedding_provider.embed_query, query, deadline=budget))
        except Exception as exc:
            _record_degraded(degraded, "EMBEDDING_UNAVAILABLE", exc)
        try:
            return await _blocking(self.index.search, SearchRequest(query=query, query_vector=vector,
                                   filters=filters, limit=max(limit, 100)), deadline=budget)
        except Exception as exc:
            _record_degraded(degraded, "SEARCH_UNAVAILABLE", exc)
            return []

    async def _apply_exclusion(self, hits, exclude_skills, budget, degraded):
        if not exclude_skills or not hits:
            return hits
        pending = [hit for hit in hits if not set(exclude_skills).issubset(hit.verified_exclusions)]
        if not pending:
            return hits
        try:
            contents = await _blocking(self.index.get_candidate_chunk_contents,
                                      list({hit.candidate_id for hit in pending}), deadline=budget)
        except Exception as exc:
            _record_degraded(degraded, "EXCLUSION_UNVERIFIED", exc)
            return []
        verified = []
        for hit in hits:
            if set(exclude_skills).issubset(hit.verified_exclusions):
                verified.append(hit)
                continue
            evidence = contents.get(hit.candidate_id)
            if not evidence:
                _record_degraded(degraded, "EXCLUSION_UNVERIFIED")
            elif not any(has_skill(content, skill) for content in evidence for skill in exclude_skills):
                verified.append(hit)
        return verified


def _apply_rerank_scored(hits, scored) -> list:
    """用模型真实相关性分数重排；校验 index、去重、补齐缺失项。"""
    score_map = {index: score for index, score in scored if isinstance(index, int)}
    order = [index for index, _ in sorted(scored, key=lambda item: item[1], reverse=True)]
    seen: set[int] = set()
    reordered: list = []
    for index in order:
        if index < 0 or index >= len(hits):
            continue
        if index in seen:
            continue
        seen.add(index)
        reordered.append(_with_rerank_score(hits[index], score_map.get(index, 0.0)))
    for index, hit in enumerate(hits):
        if index not in seen:
            reordered.append(hit)
    return reordered


def _apply_rerank_order(hits, order) -> list:
    """无真实相关性分数的降级重排：仅按顺序，不伪造 rerank_score。"""
    seen: set[int] = set()
    reordered: list = []
    for index in order:
        if not isinstance(index, int):
            continue
        if index < 0 or index >= len(hits):
            continue
        if index in seen:
            continue
        seen.add(index)
        reordered.append(hits[index])
    for index, hit in enumerate(hits):
        if index not in seen:
            reordered.append(hit)
    return reordered


def _dedupe_candidates(hits, limit: int) -> list:
    """按候选人去重后计数、截断。"""
    seen: set[str] = set()
    result: list = []
    for hit in hits:
        if hit.candidate_id in seen:
            continue
        seen.add(hit.candidate_id)
        result.append(hit)
        if len(result) >= limit:
            break
    return result


def _with_rerank_score(hit, score: float):
    from dataclasses import replace

    return replace(hit, rerank_score=round(score, 6))


def _promote_rerank_score(hit):
    """把 rerank 分回写 score，使 API 返回的分数与最终排序一致。"""
    from dataclasses import replace

    if hit.rerank_score is not None:
        return replace(hit, score=hit.rerank_score)
    return hit
