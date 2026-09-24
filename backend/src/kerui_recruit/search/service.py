from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from functools import partial

from kerui_recruit.providers.contracts import EmbeddingProvider, RerankerProvider
from kerui_recruit.search.contracts import (
    CandidateFilters,
    EvidenceChunk,
    QueryPlan,
    SearchPage,
    SearchRequest,
)
from kerui_recruit.search.lancedb_index import (
    CHANNEL_VECTOR_ORIGINAL,
    CHANNEL_VECTOR_REWRITE,
    EVIDENCE_TEXT_BUDGET,
    LanceDBSearchIndex,
    SEARCH_DEADLINE,
)
from kerui_recruit.search.lexicon import LexicalConcept, is_curated_concept, tokenize_lexical_text
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
VECTOR_MIN_SIMILARITY: float | None = None  # 纯向量模式绝对下限；None=哨兵，不设下限（见下）
VECTOR_FUSION_MIN_SIMILARITY: float | None = 0.6  # 混合模式向量通道的绝对下限 ⇔ cos 0.667
VECTOR_RELATIVE_RATIO = 0.9  # 向量相似度相对阈值：保留 >= top1 分数 × 0.9，随召回规模自适应
# 纯向量模式的绝对下限改为哨兵 None 的依据（离线实测，99 条带标签查询，见
# `.trae/documents/检索与匹配优化方案-2026-09-19.md` §3）：
# - 该常量在 3 万份规模上只裁掉 15/9900 = 0.15% 的向量召回，实测 abs=0.55 与 abs=0（哨兵）
#   在 R@20 / NDCG@10 / P@5 / 空结果 四项上**完全一致**（0.0564 / 0.3692 / 0.4303 / 0），
#   即满规模下哨兵化是无害且等价的；
# - 它的存在反而制造过事故：早期取 0.6 时把纯向量模式整体清空（11 条零结果），
#   而稀疏岗位（如 Avaloq）即使最匹配的人也够不到该线。
# 小库上相对阈值会随 top1 变松，此时由重排分（RERANK_MIN_SCORE）承担质量把关；
# 重排不可用时由 VECTOR_MIN_SIMILARITY_FALLBACK 事后兜底。三者构成完整的质量链。
# 注意：VECTOR_FUSION_MIN_SIMILARITY 保持 0.6 **不哨兵化** —— 它每轮裁掉约一半向量召回
# （实测 4945/9900 = 49.95%），是混合检索配比的一部分；且混合模式下向量相似度在 RRF
# 融合后即丢失，没有兜底路径，置为 None 会让重排失败时再无任何守卫。
VECTOR_MIN_SIMILARITY_FALLBACK = 0.5263  # ⇔ cos 0.55；重排不可用时的兜底
# 重排分下限：重排可用时由它承担质量把关；0.0 表示不设下限（现值由 §4.5 扫描定档）。
RERANK_MIN_SCORE = 0.0

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

# 重排窗口：送入 reranker 的文档数。原为硬编码 100，提取为常量以便扫描与调优。
# 实测（见方案 §4.3）：该值 <= limit 时返回集合恒等于 RRF 的 top-limit（重排只剩排序作用），
# 因此它必须 > limit 才有「选人」意义；现役 100 只用到重排信号上限的 88.96%。
RERANK_DOCS = 100

# FTS / 向量各自的最小召回数。原为硬编码 100，提取为常量以便扫描。
# 它是「隐藏旋钮」：即使 POOL_LIMIT_CAP 降下来，两路仍各召回 RECALL_MIN 条，池子缩不小。
RECALL_MIN = 100

# 阶段 4 消融档位：None = 基线（四种 kind 在同一全局 top-K 里竞争，一次取 RECALL_MIN）；
# 正整数 = 分类型召回，每种 kind 各取 N 行（实验档 20/40/60，默认不启用）。
VECTOR_KIND_RECALL_QUOTA: int | None = None

# 概念覆盖率的三种用法（阶段 4 消融档位；默认关闭，未在独立验收集达标前不启用）：
# - "off"：完全不参与排序；
# - "tiebreak"：融合分并列时的第二排序键；
# - "light"：按 CONCEPT_COVERAGE_WEIGHT 参与排序（轻权重，只影响近邻名次）。
# 三种模式都只改**排序**，不改公开 score。
CONCEPT_COVERAGE_MODE = "off"
CONCEPT_COVERAGE_WEIGHT = 0.005

# 混合通道 FTS 的弱命中过滤开关（阶段 1 消融旋钮，默认开启 = 现网行为）。
# 只在跑「hybrid × {正文开/关} × {弱过滤开/关}」A/B 定档时由脚本置 False 取对照臂；
# 生产路径不读它以外的任何入口，置 True 时行为与本常量引入前逐字一致。
HYBRID_WEAK_HIT_FILTER = True


@dataclass(frozen=True, slots=True)
class _RewriteOutcome:
    """改写结果到公开契约的映射：六值 status + applied + fallback_reason。"""

    semantic_query: str
    status: str  # disabled / not_applicable / unchanged / success / rejected / unavailable
    applied: bool = False
    fallback_reason: str | None = None  # 仅技术性原因：provider_error

# 证据槽位选择时忽略的低信息功能词与职责动词：它们几乎出现在每个片段里，
# 无法区分片段相关性（其余已策展与未策展概念同等参与）。
_EVIDENCE_STOPWORDS = frozenset({
    "负责", "熟悉", "具备", "经验", "相关", "等", "掌握", "了解", "精通", "参与",
    "能力", "工作", "要求", "优先", "具备以下", "以上", "以下", "以及", "或者",
})

# 搜索侧证据槽位名：概况 + 查询主证据 + 查询补充证据。
SEARCH_EVIDENCE_SLOTS = ("overview", "primary", "complementary")
_SEARCH_EVIDENCE_HEADERS = {
    "overview": "[概况]",
    "primary": "[查询主证据]",
    "complementary": "[查询补充证据]",
}


# 「先判断能不能硬筛，不能就退化为软排」的判定顺序（任务组 8）。
#
# 触发前提：这些条件由 **AI 智能解析**（或 JD 下推）给出，不是使用者在面板上手填的
# —— 手填的意图明确，筛空就是筛空，不该被悄悄放宽。
#
# 退化优先级 = **最不具体的先退**（2026-09-22 按 8.5 补测结论反转，用户选定）。
#
# 原先的顺序是 company → title → business_directions → career_directions →
# career_specializations，理由是「company/title 是整串子串匹配、最容易筛空，所以先丢」。
# 实测（任务组 8.5 补测）证明这条理由看错了对象：空结果率确实能靠它降到 0，但救回的名单与
# 「只去掉那条附加条件本该得到的名单」**重合度@10 中位数 0.000**（15 条可比里 14 条为零）
# —— 被丢掉的是 company/title，也就是**最具体、使用者最在意**的那两条。
# 现在反过来：三个方向字段是有限枚举、语义更宽泛，先退；company/title 最后才动。
_RELAXATION_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("career_specializations", ("career_specializations",)),
    ("career_directions", ("career_directions",)),
    ("business_directions", ("business_directions",)),
    ("title", ("title",)),
    ("company", ("company", "companies")),
)
_FILTER_DEFAULTS = asdict(CandidateFilters())
# 「可退化为软排」的字段名，供 API 层判断哪些 AI 解析条件允许退化（唯一来源）。
RELAXABLE_FIELDS: tuple[str, ...] = tuple(name for name, _ in _RELAXATION_ORDER)


def _pool_limit(limit: int) -> int:
    """内部候选池：常规分页固定 120，深翻页才按显式深分页策略扩池。

    与请求 `limit` 解耦——同一查询在 `limit=20/50/100` 下共享同一候选池，
    公共前缀的排序不会因为调用方改条数而变化。
    """
    if limit > POOL_LIMIT_CAP:
        return min(limit * POOL_LIMIT_MULTIPLIER, POOL_LIMIT_CEILING)
    return POOL_LIMIT_CAP


def _concept_token_groups(concepts: tuple[LexicalConcept, ...]) -> tuple[frozenset[str], ...]:
    """把查询概念展开成 token 组（每个概念一组），并丢掉低信息功能词。"""
    groups: list[frozenset[str]] = []
    for concept in concepts:
        if concept.canonical in _EVIDENCE_STOPWORDS:
            continue
        tokens = frozenset(
            token
            for alias in (concept.canonical, *concept.aliases)
            for token in tokenize_lexical_text(alias)
        )
        if tokens:
            groups.append(tokens)
    return tuple(groups)


def _covered_concepts(groups: tuple[frozenset[str], ...], text: str) -> frozenset[int]:
    tokens = set(tokenize_lexical_text(text))
    return frozenset(index for index, group in enumerate(groups) if tokens & group)


def _chunk_contribution(chunk: EvidenceChunk) -> float:
    return max((signal.reciprocal_rank for signal in chunk.signals), default=0.0)


def _chunk_order_key(chunk: EvidenceChunk) -> tuple[float, str]:
    """平局打破键：先融合贡献，再稳定 chunk ID（不用文本或跨通道 raw rank）。"""
    return (_chunk_contribution(chunk), chunk.chunk_id or chunk.text)


def search_evidence_pack(evidence: tuple[EvidenceChunk, ...] | list[EvidenceChunk],
                         concepts: tuple[LexicalConcept, ...]) -> dict[str, str]:
    """搜索侧证据包：概况 + 查询主证据 + 查询补充证据。

    - 概况优先取真实 `kind=parent`；没有 parent 时取贡献最高的片段，保留其原始 kind；
    - 主证据在其余片段里最大化查询概念覆盖，补充证据最大化「相对主证据的新增覆盖」；
    - 没有词法覆盖但经向量阈值进入召回的片段仍按贡献参与兜底（不因词表未收录而丢弃）；
    - 槽位不要求 kind 不同，只要求规范化文本不同；没有相关证据时留空。

    搜索侧刻意不区分「技术 / 业务」概念：现有技能词表与行业/业务方向标签的覆盖范围
    和粒度不同，临时拼成二分类会产生多标签歧义。match 侧另有结构化词表，见 `_evidence_pack`。
    """
    chunks = [chunk for chunk in evidence if chunk.text.strip()]
    if not chunks:
        return {}
    groups = _concept_token_groups(concepts)
    coverage = {id(chunk): _covered_concepts(groups, chunk.text) for chunk in chunks}

    overview = next((chunk for chunk in chunks if chunk.kind == "parent"), None)
    if overview is None:
        overview = max(chunks, key=_chunk_order_key)
    pack: dict[str, str] = {"overview": overview.text}

    rest = [chunk for chunk in chunks if chunk is not overview]
    eligible = [chunk for chunk in rest if coverage[id(chunk)] or _chunk_contribution(chunk) > 0]
    # 概况是稳定展示投影与解释基准，裁剪优先级最高（永不先被裁）。
    ranks: dict[str, tuple[int, float]] = {"overview": (len(groups) + 1, 1.0)}
    if eligible:
        primary = max(eligible, key=lambda chunk: (
            len(coverage[id(chunk)]), *_chunk_order_key(chunk)))
        pack["primary"] = primary.text
        ranks["primary"] = (len(coverage[id(primary)]), _chunk_contribution(primary))
        remaining = [chunk for chunk in eligible if chunk is not primary]
        if remaining:
            complement = max(remaining, key=lambda chunk: (
                len(coverage[id(chunk)] - coverage[id(primary)]),
                len(coverage[id(chunk)]),
                *_chunk_order_key(chunk)))
            pack["complementary"] = complement.text
            ranks["complementary"] = (len(coverage[id(complement)]), _chunk_contribution(complement))
    return _trim_evidence_pack(pack, ranks)


def _trim_evidence_pack(pack: dict[str, str], ranks: dict[str, tuple[int, float]]) -> dict[str, str]:
    """总长度超限时按「查询概念覆盖数 → 片段贡献」裁剪优先级最低的槽位。"""
    while sum(len(text) for text in pack.values()) > EVIDENCE_TEXT_BUDGET:
        droppable = [slot for slot in SEARCH_EVIDENCE_SLOTS if slot in pack and slot != "overview"]
        if not droppable:
            break
        weakest = min(droppable, key=lambda slot: (*ranks.get(slot, (0, 0.0)), slot))
        pack.pop(weakest, None)
    return pack


def render_evidence_pack(pack: dict[str, str]) -> str:
    """把证据包渲染成 reranker / prompt 的只读文本。"""
    return "\n".join(f"{_SEARCH_EVIDENCE_HEADERS[slot]}\n{pack[slot]}"
                     for slot in SEARCH_EVIDENCE_SLOTS if pack.get(slot))


def _concept_coverage(hit, curated_groups: tuple[frozenset[str], ...]) -> float | None:
    """已策展查询概念覆盖率（命中数 / 查询已策展概念数）；查询无已策展概念时为 None。

    只用于诊断与阶段 4 的软排序实验，阶段 1 不参与排序。
    """
    if not curated_groups:
        return None
    texts = [chunk.text for chunk in (hit.evidence or ())] or [hit.content]
    tokens: set[str] = set()
    for text in texts:
        tokens.update(tokenize_lexical_text(text))
    matched = sum(1 for group in curated_groups if tokens & group)
    return round(matched / len(curated_groups), 6)



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


def _record_phase(observer, diagnostics: dict | None, phase: str, started: float) -> None:
    """阶段耗时：写进诊断记录（生产日志）并可选择转发给 observer。

    生产路径 observer 为 None，所以耗时不能再只依赖 observer——否则「每次搜索记录各阶段
    耗时」在真实运行里恒为空。
    """
    elapsed_ms = (time.monotonic() - started) * 1000
    if diagnostics is not None:
        diagnostics.setdefault("phase_ms", {})[phase] = round(elapsed_ms, 3)
    if observer is not None:
        observer.record_phase(phase, elapsed_ms)


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

    async def _relax_unmatchable_filters(
        self, filters: CandidateFilters, candidates: tuple[str, ...], *,
        budget: float,
    ) -> tuple[CandidateFilters, tuple[str, ...]]:
        """**先判断这一条能不能硬筛**；不能就退化为软排。**最多只退化一条**。

        判定方式是**按字段单独试**：对每个允许退化的字段，问「只把这一条拿掉，剩下的硬条件
        还筛不筛得出人」。筛得出 → 这一条就是把结果清空的那条 → 退化它（值不出现在 where 里，
        但查询词仍在 FTS / 向量 / 重排里参与打分，即「退化为软排」）。
        多条都成立时按 `_RELAXATION_ORDER` 取**最不具体**的那条（先枚举、后 company/title）。

        **为什么不再「按顺序连环丢」**：原先的实现是固定优先级逐条丢、丢到剩下某个条件非空
        为止，判据只有「非空」、不含任何相关性判据。实测（8.5 补测，判决集 20 个 JD 与
        `.dev-data` 拼查询两套独立样本）：空结果率确实从 41.7% / 67.5% 降到 0%，但救回的名单
        与「只去掉那条附加条件本该得到的名单」**重合度@10 中位数 0.000**（15 条可比里 14 条为零），
        一次丢掉 `company + title + career_directions` 三条时救回的是「满足某个模糊方向」的人。
        数量救回来了，问题没被回答。现在只丢一条；**一条都不解决就什么都不动、如实返回空**，
        让界面说「N 条精确条件下无匹配」，而不是给一份看起来有结果的错名单。

        单字段试（而不是按顺序连环丢）是必须的：连环丢会丢掉「本来能满足的那条」。
        例：语料里只有「技术方向=TECH_BACKEND」的人，[company=不存在, 方向=TECH_BACKEND]
        两条一起筛空 —— 按顺序先丢方向，剩下 [company=不存在] 依旧是空；单字段试会先发现
        「只丢 company」能筛出人。

        探测走 ``index.filter_search``（纯元数据过滤，不产生 embedding、不花重排钱），
        最多 1 + 5 次，所以比「整轮检索发现是 0 再回退」便宜得多。

        失败开放：探测本身抛错时按「还有结果」处理（即什么都不退化）。没有证据时
        放宽条件，等于把使用者明确表达的条件悄悄丢掉——那比 0 结果更糟。
        """
        # 全条件一起还有结果 → 一条都不用退（也避免「本来就非空却被退掉一条」）。
        if await self._filter_probe(filters, budget):
            return filters, ()
        present = [
            (name, fields) for name, fields in _RELAXATION_ORDER
            if name in candidates and any(getattr(filters, f) for f in fields)
        ]
        for name, fields in present:
            reduced = replace(filters, **{f: _FILTER_DEFAULTS[f] for f in fields})
            if await self._filter_probe(reduced, budget):
                return reduced, (name,)
        return filters, ()

    async def _filter_probe(self, filters: CandidateFilters, budget: float) -> bool:
        """这套硬条件在索引里还筛不筛得出人。探测失败按 True（不退化）处理。"""
        try:
            rows = await _blocking(self.index.filter_search, filters, 1, deadline=budget)
        except Exception:
            return True
        return bool(rows)

    async def search(self, query: str, filters: CandidateFilters, *, limit: int,
                     mode: str = "hybrid",
                     deadline: float | None = None,
                     observer: SearchObserver | None = None,
                     vector_query: str | None = None,
                     operator: str = "smart",
                     concepts: tuple[LexicalConcept, ...] = (),
                     rewrite_enabled: bool = False,
                     search_body: bool = False,
                     semantic_query: str | None = None,
                     relaxable_fields: tuple[str, ...] = ()) -> SearchPage:
        budget = min(deadline, time.monotonic() + self.search_timeout) if deadline is not None else time.monotonic() + self.search_timeout
        degraded: list[str] = []
        relaxed: tuple[str, ...] = ()
        outcome = _RewriteOutcome(
            vector_query or query,
            "disabled" if not rewrite_enabled else "unavailable",
            fallback_reason=None if not rewrite_enabled else "provider_error",
        )
        diagnostics: dict = {
            "mode": mode,
            "operator": operator,
            "query": query,
            "query_sha": hashlib.sha256(
                " ".join(query.split()).casefold().encode("utf-8")).hexdigest()[:16],
            "rewrite_enabled": rewrite_enabled,
            "search_body": search_body,
            "limit": limit,
        }

        def _plan() -> QueryPlan:
            if mode == "keyword" or not query.strip():
                status = "not_applicable" if mode == "keyword" else "disabled"
                return QueryPlan(operator=operator, rewrite_requested=rewrite_enabled,
                                 rewrite_status=status, semantic_query=None, relaxed=relaxed)
            if not rewrite_enabled and not (semantic_query or "").strip():
                return QueryPlan(operator=operator, rewrite_requested=rewrite_enabled,
                                 rewrite_status="disabled", semantic_query=None, relaxed=relaxed)
            # 语义查询可能来自 LLM 解析（parse_enabled）或独立改写；两者都表示「语义查询已生效」。
            return QueryPlan(operator=operator, rewrite_requested=rewrite_enabled,
                             rewrite_status=outcome.status,
                             semantic_query=outcome.semantic_query if outcome.applied else None,
                             rewrite_applied=outcome.applied,
                             rewrite_fallback_reason=outcome.fallback_reason,
                             relaxed=relaxed)

        def _page(items, *, empty_reason=None) -> SearchPage:
            self._log_diagnostics(diagnostics, items, degraded, observer)
            return SearchPage(items=tuple(items), degraded_reasons=tuple(dict.fromkeys(degraded)),
                              empty_reason=empty_reason, query_plan=_plan(), relaxed=relaxed,
                              effective_filters=filters if relaxed else None)

        try:
            ready = await _blocking(self.index.is_ready, deadline=budget)
        except TimeoutError:
            _record_degraded(degraded, "TIMEOUT")
            return _page((), empty_reason="service_error")
        except Exception as exc:
            _record_degraded(degraded, "SEARCH_UNAVAILABLE", exc)
            return _page((), empty_reason="service_error")
        if not ready:
            return _page((), empty_reason="index_not_ready")

        pool_limit = _pool_limit(limit)
        diagnostics["pool_limit"] = pool_limit
        if relaxable_fields:
            # 任务组 8：先判断这些条件能不能硬筛，不能就先退化为软排，再进入召回。
            # 放在召回之前而不是「召回为空再回退」：探测是纯元数据过滤，比跑一轮注定
            # 为空的向量召回 + 重排便宜得多，也不会把 0 结果当成结论交给上层。
            filters, relaxed = await self._relax_unmatchable_filters(
                filters, relaxable_fields, budget=budget)
            if relaxed:
                for name in relaxed:
                    _record_degraded(degraded, f"FILTER_RELAXED:{name}")
                diagnostics["relaxed_filters"] = list(relaxed)
        if not query.strip():
            try:
                hits = await _blocking(self.index.filter_search, filters, limit, deadline=budget)
            except Exception as exc:
                reason = "EXCLUSION_UNVERIFIED" if filters.exclude_skills else "SEARCH_UNAVAILABLE"
                _record_degraded(degraded, reason, exc)
                return _page((), empty_reason="service_error")
        elif mode == "keyword":
            hits = await self._keyword_retrieve(query, filters, pool_limit, budget, degraded, observer, operator=operator, concepts=concepts, search_body=search_body, diagnostics=diagnostics)
        elif mode == "vector":
            hits, outcome = await self._vector_retrieve(
                vector_query or query, filters, pool_limit, budget, degraded, observer,
                rewrite_enabled=rewrite_enabled, semantic_query=semantic_query,
                diagnostics=diagnostics)
        elif hasattr(self.index, "search_fts") and hasattr(self.index, "fuse"):
            hits, outcome = await self._parallel_retrieve(
                query, filters, pool_limit, budget, degraded, observer, vector_query,
                rewrite_enabled=rewrite_enabled, search_body=search_body, concepts=concepts,
                semantic_query=semantic_query, diagnostics=diagnostics)
        else:
            hits = await self._legacy_retrieve(query, filters, pool_limit, budget, degraded, observer)

        hits = await self._apply_exclusion(hits, filters.exclude_skills, budget, degraded)
        if not hits:
            return _page((), empty_reason="service_error" if degraded else "no_match")
        if any(hit.representative_kind != "parent" for hit in hits):
            # 索引里确实没有 parent 行才会走到这里：展示回退已发生，但融合结果仍然保留。
            _record_degraded(degraded, "PARENT_CHUNK_MISSING")
        diagnostics["chunk_kinds"] = _kind_distribution(hits)
        diagnostics["pool_size"] = len(hits)
        hits = _with_concept_coverage(hits, _curated_concept_tokens(concepts))
        hits = _apply_concept_coverage_mode(hits)
        scored_ok = False
        if query.strip():
            # 纯关键词模式不使用语义重排，避免改变词法排序。
            if mode != "keyword" and time.monotonic() < budget:
                try:
                    rerank_query = vector_query or query
                    window = hits[:RERANK_DOCS]
                    contents = [_rerank_text(hit, concepts) for hit in window]
                    _t = time.monotonic()
                    if hasattr(self.reranker_provider, "rerank_scored"):
                        scored = await self._provider(self.reranker_provider.rerank_scored, rerank_query, contents, deadline=budget)
                        hits = _apply_rerank_scored(hits, scored)
                        # 把 rerank 分回写 score，使 API 返回的 score 与最终排序一致。
                        hits = [_promote_rerank_score(hit) for hit in hits]
                        scored_ok = True
                        diagnostics["reranker"] = "scored"
                    else:
                        order = await self._provider(self.reranker_provider.rerank, rerank_query, contents, deadline=budget)
                        hits = _apply_rerank_order(hits, order)
                        diagnostics["reranker"] = "order"
                    _record_phase(observer, diagnostics, "rerank", _t)
                except Exception as exc:
                    _record_degraded(degraded, "RERANKER_UNAVAILABLE", exc)
                    diagnostics["reranker"] = "unavailable"
            elif mode != "keyword":
                _record_degraded(degraded, "TIMEOUT")
                diagnostics["reranker"] = "timeout"
        else:
            diagnostics["reranker"] = "skipped"
        # 第三阶段：质量把关。拿到重排分就由重排分承担；没拿到（限流/异常/仅顺序重排）
        # 则回落到绝对下限 —— 这是「绝对阈值哨兵化」唯一实质风险的兜底。
        if query.strip() and mode != "keyword":
            hits = (_apply_rerank_min_score(hits, RERANK_MIN_SCORE) if scored_ok
                    else _apply_vector_absolute_fallback(hits, mode))
        hits = _dedupe_candidates(hits, limit)
        diagnostics["returned"] = len(hits)
        return _page(hits)

    @staticmethod
    def _log_diagnostics(diagnostics: dict, items, degraded: list[str], observer) -> None:
        """每次搜索记录一条脱敏诊断（生产日志 + 可选 observer）。

        只记检索过程指标（通道召回数、阈值前后数量、chunk 类型分布、融合池大小、
        重排状态与降级原因），不记简历正文、手机号等候选人隐私。
        """
        payload = dict(diagnostics)
        payload["degraded"] = list(dict.fromkeys(degraded))
        if items:
            payload["top_channels"] = list(items[0].matched_channels)
        if observer is not None:
            observer.record_diagnostics(payload)
        logger.info("search diagnostics %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))

    async def _maybe_rewrite(self, query: str, rewrite_enabled: bool, budget: float) -> _RewriteOutcome:
        """在子预算内改写；任何失败都回退原查询，且不产生查询向量 B。

        公开六值状态与文档一致：disabled / not_applicable / unchanged / success /
        rejected / unavailable；``rewrite_fallback_reason`` 只再补充技术性原因（provider_error）。
        """
        if not rewrite_enabled:
            return _RewriteOutcome(query, "disabled")
        if self.rewriter is None:
            return _RewriteOutcome(query, "unavailable", fallback_reason="provider_error")
        rewrite_deadline = min(budget, time.monotonic() + min(5.0, max(0.0, (budget - time.monotonic()) * 0.6)))
        try:
            result = await self._provider(self.rewriter.rewrite, query, deadline=rewrite_deadline)
        except Exception as exc:
            # 改写失败不影响对外原因码（rewrite_status 仍是 unavailable），但不能再无声吞掉。
            logger.warning(
                "search rewrite degraded error=%s code=%s category=%s request_id=%s",
                type(exc).__name__,
                getattr(exc, "code", None),
                getattr(exc, "category", None),
                getattr(exc, "request_id", None),
            )
            return _RewriteOutcome(query, "unavailable", fallback_reason="provider_error")
        outcome = getattr(result, "outcome", None) or getattr(result, "status", None)
        rewritten = getattr(result, "query", None)
        if outcome in ("changed", "success") and rewritten and rewritten != query:
            return _RewriteOutcome(rewritten, "success", applied=True)
        if outcome == "unchanged":
            return _RewriteOutcome(query, "unchanged")
        if outcome == "rejected":
            return _RewriteOutcome(query, "rejected")
        return _RewriteOutcome(query, "unavailable", fallback_reason="provider_error")

    async def _semantic_outcome(self, query: str, semantic_query: str | None, rewrite_enabled: bool, budget: float):
        """语义查询来源优先级：LLM 解析产物 > 独立改写调用（二者同时开启时只花一次调用）。"""
        prepared = (semantic_query or "").strip()
        if prepared and prepared != query:
            return _RewriteOutcome(prepared, "success", applied=True)
        return await self._maybe_rewrite(query, rewrite_enabled, budget)

    async def _keyword_retrieve(self, query, filters, limit, budget, degraded, observer=None,
                                operator="smart", concepts=(), search_body=False, diagnostics=None):
        """仅关键词召回，不调用 embedding，不使用语义重排。"""
        if not hasattr(self.index, "search_fts"):
            _record_degraded(degraded, "FTS_UNAVAILABLE")
            return []
        if operator in ("and", "or") and concepts and hasattr(self.index, "search_fts_boolean"):
            _t = time.monotonic()
            try:
                rows = await _blocking(self.index.search_fts_boolean, query, concepts, operator,
                                       filters, limit, search_body, deadline=budget)
                _record_phase(observer, diagnostics, "fts", _t)
                if diagnostics is not None:
                    diagnostics["fts_rows"] = len(rows)
                return self.index.hits_from_rows(rows, "bm25")
            except Exception as exc:
                _record_degraded(degraded, "FTS_UNAVAILABLE", exc)
                return []
        _t = time.monotonic()
        try:
            rows = await _blocking(self.index.search_fts, query, filters, limit, search_body, deadline=budget)
            rows = self._filter_by_hit_terms(rows, query, search_body=search_body)
            _record_phase(observer, diagnostics, "fts", _t)
            if diagnostics is not None:
                diagnostics["fts_rows"] = len(rows)
            return self.index.hits_from_rows(rows, "bm25")
        except Exception as exc:
            _record_degraded(degraded, "FTS_UNAVAILABLE", exc)
            return []

    @classmethod
    def _filter_by_hit_terms(cls, rows, query, *, search_body: bool = False):
        """关键词 smart 模式：过滤与查询词毫无交集的弱结果。

        ``search_body=True`` 时正文也计入词条（父行正文只落在 ``body_index_text``）。
        """
        terms = set(tokenize_lexical_text(query))
        if len(terms) <= KEYWORD_MIN_HIT_TERMS:
            return rows
        kept = []
        for row in rows:
            if len(terms & cls._row_terms(row, search_body)) >= KEYWORD_MIN_HIT_TERMS:
                kept.append(row)
        return kept

    @staticmethod
    def _curated_concept_aliases(concepts) -> set[str]:
        """查询里已策展概念的别名集合（技能/同义词）；通用词不计入。"""
        aliases: set[str] = set()
        for concept in concepts or ():
            if not is_curated_concept(concept.canonical):
                continue
            aliases.update(alias.casefold() for alias in concept.aliases if alias)
        return aliases

    def _filter_hybrid_fts(self, rows, query, concepts, diagnostics, *, search_body: bool = False) -> list:
        """混合通道的 FTS 弱命中过滤：先按命中词条过滤，再要求候选人命中 ≥1 个已策展概念。

        动机：正文开关打开后，正文里出现的单个泛词会把大量噪声行送进 RRF 融合池与重排窗口
        （融合只看 rank，不看分数）。两道过滤都是"弱命中"口径，不改变排序：
        - 与查询词条毫无交集的行走掉（与关键词模式同一实现）；正文打开时词条以
          ``keyword_index_text ∪ body_index_text`` 计——父行的正文只落在 body_index_text，
          只看 keyword_index_text 会把「只命中正文」的父行误杀；
        - 候选人级聚合：该候选人任一 chunk 命中 ≥1 个已策展概念才保留其全部命中行；
          查询本身没有已策展概念（纯通用词查询）时自动跳过这一道，避免清空召回。
        """
        before = len(rows)
        if not HYBRID_WEAK_HIT_FILTER:
            # 对照臂：不过滤，直接把 FTS 原始行交给 RRF（仅用于阶段 1 A/B 定档）。
            if diagnostics is not None:
                diagnostics["hybrid_fts_rows_before_filter"] = before
                diagnostics["hybrid_fts_rows_after_hit_terms"] = before
                diagnostics["hybrid_fts_rows_after_concept"] = before
                diagnostics["hybrid_fts_concept_gate"] = False
                diagnostics["hybrid_fts_gate_emptied"] = False
                diagnostics["hybrid_fts_weak_filter"] = False
            return rows
        rows = self._filter_by_hit_terms(rows, query, search_body=search_body)
        after_terms = len(rows)
        aliases = self._curated_concept_aliases(concepts)
        gate_applied = bool(aliases)
        gate_emptied = False
        if gate_applied:
            matched: dict[str, bool] = {}
            for row in rows:
                candidate_id = row.get("candidate_id")
                if matched.get(candidate_id):
                    continue
                matched[candidate_id] = bool(self._row_terms(row, search_body) & aliases)
            kept = [row for row in rows if matched.get(row.get("candidate_id"))]
            if kept:
                rows = kept
            else:
                # 失败开放：闸门会把通道清空时退回原集合，绝不把「非空」变「空」。
                # 触发条件是「查询的已策展概念在全池无人命中」（实测 `Avaloq 开发` 这类写法：
                # 全池无一人具备 Avaloq），此时闸门滤掉全部 FTS 行，而向量通道又可能被相对
                # 阈值滤空，整页随之变空。与 `_apply_rerank_min_score` /
                # `_apply_vector_absolute_fallback` 是同一纪律：质量闸门用于压尾部噪声，
                # 不能把整页清空。
                gate_emptied = True
        if diagnostics is not None:
            diagnostics["hybrid_fts_rows_before_filter"] = before
            diagnostics["hybrid_fts_rows_after_hit_terms"] = after_terms
            diagnostics["hybrid_fts_rows_after_concept"] = len(rows)
            diagnostics["hybrid_fts_concept_gate"] = gate_applied
            diagnostics["hybrid_fts_gate_emptied"] = gate_emptied
            diagnostics["hybrid_fts_weak_filter"] = True
        return rows

    @staticmethod
    def _row_terms(row, search_body: bool) -> set[str]:
        """一行可参与词条/概念匹配的词：正文打开时含父行的 body_index_text。"""
        text = row.get("keyword_index_text") or ""
        if search_body:
            text = f"{text} {row.get('body_index_text') or ''}"
        return {token.casefold() for token in text.split()}

    async def _embed_query(self, text: str, budget: float, degraded: list[str]) -> tuple | None:
        """查询向量；失败返回 None，由调用方决定是降级还是报错。"""
        try:
            return tuple(await self._provider(self.embedding_provider.embed_query, text, deadline=budget))
        except Exception as exc:
            _record_degraded(degraded, "EMBEDDING_UNAVAILABLE", exc)
            return None

    async def _vector_retrieve(self, query, filters, limit, budget, degraded, observer=None,
                               rewrite_enabled=False, diagnostics=None, semantic_query=None):
        """仅向量召回：原查询向量 A 始终存在，改写向量 B 只作补充。

        B 的 embedding 失败只丢弃 B，不影响 A 的结果。
        """
        outcome = await self._semantic_outcome(query, semantic_query, rewrite_enabled, budget)
        _t_embed = time.monotonic()
        original = await self._embed_query(query, budget, degraded)
        _record_phase(observer, diagnostics, "embedding", _t_embed)
        if original is None:
            return [], outcome
        rewrite_rows: list = []
        _t_vector = time.monotonic()
        try:
            original_rows = await _blocking(
                partial(self.index.search_vector, original, filters, limit,
                        channel=CHANNEL_VECTOR_ORIGINAL, kind_quota=VECTOR_KIND_RECALL_QUOTA),
                deadline=budget)
            if outcome.applied:
                rewritten = await self._embed_query(outcome.semantic_query, budget, degraded)
                if rewritten is not None:
                    rewrite_rows = await _blocking(
                        partial(self.index.search_vector, rewritten, filters, limit,
                                channel=CHANNEL_VECTOR_REWRITE, kind_quota=VECTOR_KIND_RECALL_QUOTA),
                        deadline=budget)
            hits = self.index.fuse([], original_rows, limit, rewrite_rows)
            hits = _with_vector_similarity(hits)
            hits = self._apply_vector_threshold(hits)
            if diagnostics is not None:
                diagnostics["vector_original_rows"] = len(original_rows)
                diagnostics["vector_rewrite_rows"] = len(rewrite_rows)
                diagnostics["vector_rows_before_threshold"] = len(original_rows) + len(rewrite_rows)
                diagnostics["vector_rows_after_threshold"] = len(hits)
            return hits, outcome
        except Exception as exc:
            _record_degraded(degraded, "VECTOR_UNAVAILABLE", exc)
            return [], outcome
        finally:
            _record_phase(observer, diagnostics, "vector", _t_vector)

    @staticmethod
    def _apply_vector_threshold(hits: list) -> list:
        """向量召回相对阈值：保留 >= top1 分数 × VECTOR_RELATIVE_RATIO 的结果。

        绝对下限 VECTOR_MIN_SIMILARITY 为哨兵 ``None`` 时不再参与，只保留相对阈值。
        此时「top1 本身很低、相对阈值跟着变松」的风险由重排分承担；重排不可用时
        由 ``VECTOR_MIN_SIMILARITY_FALLBACK`` 事后兜底（见 ``search``）。
        """
        if not hits:
            return hits
        top_score = max(hit.score for hit in hits)
        threshold = top_score * VECTOR_RELATIVE_RATIO
        if VECTOR_MIN_SIMILARITY is not None:
            threshold = max(VECTOR_MIN_SIMILARITY, threshold)
        return [hit for hit in hits if hit.score >= threshold]

    async def _parallel_retrieve(self, query, filters, limit, budget, degraded, observer=None,
                                 vector_query=None, rewrite_enabled=False, search_body=False,
                                 concepts=(), diagnostics=None, semantic_query=None):
        """混合召回：FTS 用原关键词，向量通道用原查询向量 A + 可选改写向量 B。

        FTS 通道用 ``query``（词法关键词），向量通道用 ``vector_query or query``：
        反向匹配会把候选人画像文本作为 ``vector_query`` 传入，此时它才是向量与重排的原查询。

        FTS 分支与关键词模式同口径做弱命中过滤（见 ``_filter_hybrid_fts``）：正文打开后
        只命中一个泛词的行不再进入 RRF 融合池与重排窗口。
        """
        original_query = vector_query or query
        outcome = _RewriteOutcome(original_query, "disabled")
        recall = max(limit, RECALL_MIN)

        async def run_fts():
            _t = time.monotonic()
            try:
                rows = await _blocking(self.index.search_fts, query, filters, recall, search_body,
                                       deadline=budget)
                return self._filter_hybrid_fts(rows, query, concepts, diagnostics,
                                               search_body=search_body)
            finally:
                _record_phase(observer, diagnostics, "fts", _t)

        async def semantic():
            nonlocal outcome
            outcome = await self._semantic_outcome(original_query, semantic_query, rewrite_enabled, budget)
            _t_embed = time.monotonic()
            original = await self._embed_query(original_query, budget, degraded)
            if original is None:
                return []
            rewrite_rows: list = []
            if outcome.applied:
                rewritten = await self._embed_query(outcome.semantic_query, budget, degraded)
                if rewritten is not None:
                    _t_rewrite = time.monotonic()
                    try:
                        rewrite_rows = await _blocking(
                            partial(self.index.search_vector, rewritten, filters, recall,
                                    channel=CHANNEL_VECTOR_REWRITE,
                                    kind_quota=VECTOR_KIND_RECALL_QUOTA),
                            deadline=budget)
                    except Exception as exc:
                        _record_degraded(degraded, "VECTOR_UNAVAILABLE", exc)
                        rewritten = None
                    finally:
                        _record_phase(observer, diagnostics, "vector_rewrite", _t_rewrite)
                    if rewritten is None:
                        outcome = _RewriteOutcome(original_query, "unavailable",
                                                  fallback_reason="provider_error")
            _record_phase(observer, diagnostics, "embedding", _t_embed)
            _t_vector = time.monotonic()
            try:
                rows = await _blocking(
                    partial(self.index.search_vector, original, filters, recall,
                            channel=CHANNEL_VECTOR_ORIGINAL, kind_quota=VECTOR_KIND_RECALL_QUOTA),
                    deadline=budget)
                # 混合通道向量召回加相对相似度阈值，过滤低相似度噪声（与纯向量模式一致）。
                scored = [r for r in rows if isinstance(r.get("_distance"), (int, float))]
                if scored:
                    top = max(1.0 / (1.0 + r["_distance"]) for r in scored)
                    # 混合通道的下限比纯向量模式更高：这里的作用只是给 RRF 供料，而 RRF 只取
                    # rank 不看分数，放低会让低相似度行挤占送入 reranker 的 top-100 名额。
                    # 哨兵 None 时不设绝对下限，只保留相对阈值（同样由重排分承担质量把关）。
                    threshold = top * VECTOR_RELATIVE_RATIO
                    if VECTOR_FUSION_MIN_SIMILARITY is not None:
                        threshold = max(VECTOR_FUSION_MIN_SIMILARITY, threshold)
                    rows = [r for r in scored if 1.0 / (1.0 + r["_distance"]) >= threshold]
                else:
                    rows = []
                if diagnostics is not None:
                    diagnostics["vector_rows_before_threshold"] = len(scored)
                    diagnostics["vector_rows_after_threshold"] = len(rows)
                    diagnostics["vector_rewrite_rows"] = len(rewrite_rows)
                return rows, rewrite_rows
            except Exception as exc:
                _record_degraded(degraded, "VECTOR_UNAVAILABLE", exc)
                return [], rewrite_rows
            finally:
                _record_phase(observer, diagnostics, "vector", _t_vector)

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
            fts_rows = rows[0]
            vector_rows, rewrite_rows = rows[1] if rows[1] else ([], [])
            if diagnostics is not None:
                diagnostics["fts_rows"] = len(fts_rows)
            _t_fuse = time.monotonic()
            fused = self.index.fuse(fts_rows, vector_rows, recall, rewrite_rows)
            _record_phase(observer, diagnostics, "fusion", _t_fuse)
            return fused, outcome
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


def _apply_rerank_min_score(hits, minimum: float) -> list:
    """重排可用时的质量把关：丢弃低于 ``RERANK_MIN_SCORE`` 的行。

    只对**确实拿到重排分**的行生效。超出重排窗口、未被重排的行没有分数，
    不能按分数淘汰（那会等于用「没重排」当淘汰理由），保持原样交给后续
    按候选人去重与截断处理。

    全被淘汰时退回原集合：质量线用于压尾部噪声，不应该把整页清空
    （历史上绝对阈值清空纯向量模式正是靠「不返回空」这条纪律纠正的）。
    """
    if minimum <= 0:
        return hits
    kept = [hit for hit in hits if getattr(hit, "rerank_score", None) is None
            or hit.rerank_score >= minimum]
    return kept if kept else hits


def _apply_vector_absolute_fallback(hits, mode: str) -> list:
    """重排不可用时的兜底：回落到绝对下限。

    仅**纯向量模式**可兜底 —— 该模式下 ``hit.score`` 就是向量相似度。混合模式的
    ``hit.score`` 在融合后已是 RRF 分，向量相似度在该步丢失，无法事后判定。

    因此 ``VECTOR_FUSION_MIN_SIMILARITY`` **不应哨兵化**：它没有兜底路径，
    一旦置为 None，重排失败时就再无任何守卫。此约束有代码结构上的依据，不是保守。

    同样遵守「不返回空」：全被淘汰时退回原集合。
    """
    if mode != "vector" or VECTOR_MIN_SIMILARITY_FALLBACK <= 0:
        return hits
    kept = [hit for hit in hits if hit.score >= VECTOR_MIN_SIMILARITY_FALLBACK]
    return kept if kept else hits


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


def _kind_distribution(hits) -> dict[str, int]:
    """融合池里的 chunk 类型分布（脱敏诊断用，只统计条数）。"""
    counts: dict[str, int] = {}
    for hit in hits:
        for chunk in hit.evidence or ():
            counts[chunk.kind] = counts.get(chunk.kind, 0) + 1
    return dict(sorted(counts.items()))


def _curated_concept_tokens(concepts: tuple[LexicalConcept, ...]) -> tuple[frozenset[str], ...]:
    """只取已策展概念（技能 + 通用同义词）的 token 组，用于可比较的覆盖率特征。"""
    return tuple(
        frozenset(token for alias in (concept.canonical, *concept.aliases)
                  for token in tokenize_lexical_text(alias))
        for concept in concepts if is_curated_concept(concept.canonical)
    )


def _with_concept_coverage(hits, curated_groups: tuple[frozenset[str], ...]) -> list:
    return [replace(hit, concept_coverage=_concept_coverage(hit, curated_groups)) for hit in hits]


def _apply_concept_coverage_mode(hits: list) -> list:
    """阶段 4 消融：概念覆盖率作为排序特征（默认 off，完全不参与排序）。

    只改排序，不改公开 score。关键词模式没有融合分（``fusion_score`` 为空），
    覆盖率也就无从比较，因此原样返回。
    """
    if CONCEPT_COVERAGE_MODE == "off" or any(hit.fusion_score is None for hit in hits):
        return hits
    if CONCEPT_COVERAGE_MODE == "light":
        return sorted(hits, key=lambda hit: (
            -(hit.fusion_score + CONCEPT_COVERAGE_WEIGHT * (hit.concept_coverage or 0.0)),
            hit.candidate_id))
    return sorted(hits, key=lambda hit: (
        -hit.fusion_score, -(hit.concept_coverage or 0.0), hit.candidate_id))


def _rerank_text(hit, concepts: tuple[LexicalConcept, ...]) -> str:
    """reranker 输入：候选人证据包（概况 + 查询主证据 + 查询补充证据）。

    不再使用单条 ``vector_text`` —— 触发向量召回的 child 片段现在能被重排看到。
    """
    rendered = render_evidence_pack(search_evidence_pack(hit.evidence, concepts))
    return rendered or hit.vector_text or hit.content


def _with_vector_similarity(hits) -> list:
    """纯向量模式：公开 score 回到向量相似度。

    融合顺序仍由 rank contribution 决定，但该模式的绝对下限兜底
    （``VECTOR_MIN_SIMILARITY_FALLBACK``）语义要求 ``hit.score`` 就是相似度。
    """
    return [replace(hit, score=max(hit.vector_original_score or 0.0, hit.vector_rewrite_score or 0.0))
            for hit in hits]


def _with_rerank_score(hit, score: float):
    from dataclasses import replace

    return replace(hit, rerank_score=round(score, 6))


def _promote_rerank_score(hit):
    """把 rerank 分回写 score，使 API 返回的分数与最终排序一致。"""
    from dataclasses import replace

    if hit.rerank_score is not None:
        return replace(hit, score=hit.rerank_score)
    return hit
