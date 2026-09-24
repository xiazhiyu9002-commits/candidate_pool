from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.bd_agent.evidence import (
    EvidenceDoc,
    EvidenceExtractor,
    RankedChunk,
    _CAREERS_PATH_MARKERS,
    _RECRUITMENT_SOURCE_KEYWORDS,
    source_quality,
)
from kerui_recruit.bd_agent.fetcher import WebFetcher
from kerui_recruit.bd_agent.planner import QueryPlanner
from kerui_recruit.bd_agent.synthesis import SynthesisGenerator, SynthesisResult
from kerui_recruit.bd_search.service import BdSearchService, WebSearchProvider
from kerui_recruit.db.models import BdEvidence, BdLead, BdSearchSession
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.errors import ProviderError

# 单阶段超时（秒）：任何一步挂死都不能拖住整轮；超时即用已攒到的证据继续。
# 与匹配复核同一套纪律（那边是 _REVIEW_CALL_TIMEOUT_SECONDS）。
_PLAN_TIMEOUT_SECONDS = 15.0
_RETRIEVE_TIMEOUT_SECONDS = 35.0  # 一轮「搜索 + 抓取」的整体上限
_FETCH_PER_URL_TIMEOUT_SECONDS = 10.0  # 单个页面抓取上限，超时改用搜索摘要
# 线索综合走的是**强制思考**模型（见 runtime 里 SynthesisGenerator 的
# ReasoningMode.REQUIRED），要把最多 10 个证据片段读成带引用的 JSON。
# 实测火山引擎 deepseek-v4-pro 在这一步稳定超过 60 秒：原来 60 秒的上限会把
# 每一次 BD 检索都掐断，表现为「重排之后就结束、没有数据」。
_SYNTHESIS_TIMEOUT_SECONDS = 180.0

# 单次 run 的总预算：交互场景优先「最快给出可用结果」，到点就用已攒线索收尾，
# 不再开启新的一轮。必须大于 _SYNTHESIS_TIMEOUT_SECONDS，否则综合永远拿不到完整
# 预算（min(上限, 剩余) 会先被总预算掐掉），降级原因也就失去意义。
_DEFAULT_DEADLINE_SECONDS = 240.0
# 每轮最多抓取正文的页面数（按来源质量排序取前 K），其余复用搜索摘要。
_DEFAULT_FETCH_TOP_K = 8


_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "股份公司",
    "集团公司",
    "集团",
    "研究院",
)


def _normalize_company(company: str) -> str:
    """Strip common legal suffixes and case-fold for dedup comparison."""
    text = company.strip().casefold()
    for suffix in _COMPANY_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.strip()


# 从文本里回捞公司名时只认**强后缀**。裸的「科技 / 网络 / 技术」不能进这张表：
# 「算法技术」「数据网络」这类普通词组会被匹配成公司名。
_COMPANY_NAME_PATTERN = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}?"
    r"(?:股份有限公司|有限责任公司|有限公司|集团|公司|银行|研究院|事务所|工作室))"
)
# 招聘页/搜索结果的两种常见写法：①「【公司名】岗位…」②「公司名招聘岗位…」。
_TITLE_BRACKET_PATTERN = re.compile(r"^[【\[](.+?)[】\]]")
_TITLE_HIRING_PATTERN = re.compile(r"^(.{2,30}?)(?:诚聘|招聘|招贤|急聘)")
# 只有形如 careers.example.com / jobs.example.com，或路径带 /careers /jobs 时，
# 才敢拿域名当公司名来源。
_CAREERS_HOST_LABELS = frozenset({
    "careers", "career", "jobs", "job", "hr", "talent", "recruit", "join",
})
# 回捞出的名字如果只剩这些泛称，说明捞错了，宁可返回 None。
_PLACEHOLDER_STEMS = frozenset({
    "", "我们", "我司", "本", "该", "贵", "这家", "那家", "一家", "每", "各", "某", "大", "小",
    "岗位", "职位", "人才", "招聘", "诚聘", "急聘",
})

_COMPANY_TAIL_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团公司", "公司", "集团",
    "银行", "研究院", "事务所", "工作室",
)


def _company_stem(name: str) -> str:
    """去掉公司后缀后的主干，用于判断这个名字是不是泛称。"""
    for suffix in _COMPANY_TAIL_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _clean_company_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("【】[]（）()《》<>|｜-—_·,，.。:：;；\"'“”")
    cleaned = " ".join(cleaned.split())
    if not cleaned or _company_stem(cleaned) in _PLACEHOLDER_STEMS:
        return None
    return cleaned


def _company_from_text(text: str | None) -> str | None:
    """从标题或摘要里用确定性规则回捞公司名；捞不到返回 None。

    刻意保守：宁可返回 None 交回「未识别公司」，也不把岗位名、泛称当公司名写进库。
    """
    if not text:
        return None
    value = " ".join(text.split())
    bracket = _TITLE_BRACKET_PATTERN.match(value)
    if bracket:
        return _clean_company_name(bracket.group(1))
    hiring = _TITLE_HIRING_PATTERN.match(value)
    if hiring:
        return _clean_company_name(hiring.group(1))
    matched = _COMPANY_NAME_PATTERN.search(value)
    if matched:
        return _clean_company_name(matched.group(1))
    return None


def _company_from_url(url: str | None) -> str | None:
    """公司官网招聘页的域名兜底（careers.bytedance.com → bytedance）。

    招聘平台的域名（BOSS/猎聘/脉脉…）一律不参与：它们的域名里没有雇主信息。
    """
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if not host or any(board in host for board in _RECRUITMENT_SOURCE_KEYWORDS):
        return None
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    path = (parsed.path or "").lower()
    looks_like_careers = labels[0] in _CAREERS_HOST_LABELS or any(
        marker in path for marker in _CAREERS_PATH_MARKERS
    )
    if not looks_like_careers:
        return None
    candidate = labels[-2]
    if candidate in _CAREERS_HOST_LABELS or len(candidate) < 2:
        return None
    return candidate


def _recover_company_name(
    *, title: str | None, url: str | None, snippet: str | None
) -> str | None:
    """模型没给出公司名时的确定性回捞：标题 → 摘要 → 域名。

    域名排最后：它只能给出一个裸标签（``bytedance``），可信度低于正文里带公司后缀的名字。
    """
    return (
        _company_from_text(title)
        or _company_from_text(snippet)
        or _company_from_url(url)
    )


@dataclass(frozen=True, slots=True)
class AgentResult:
    session_id: str
    leads: list[BdLead]
    # 非空表示这一轮不是「正常完成」：线索可能为空是**因为流程被降级**，而不是没搜到。
    # 前端据此把「暂无线索」和「综合失败」分开显示，不再让失败伪装成空结果。
    degraded_reason: str | None = None


class BdAgent:
    """Orchestrate the multi-step deep BD search: plan -> search -> fetch ->
    rank evidence -> synthesize cited leads, with a round/query budget and a
    graceful fallback to the simple search when the LLM is unavailable."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        search_provider: WebSearchProvider,
        fetcher: WebFetcher,
        encryption: EncryptionService,
        planner: QueryPlanner | None = None,
        evidence_extractor: EvidenceExtractor | None = None,
        synthesizer: SynthesisGenerator | None = None,
        fallback: BdSearchService | None = None,
        max_rounds: int = 3,
        max_queries: int = 8,
        min_trusted_leads: int = 5,
        search_concurrency: int = 4,
        fetch_concurrency: int = 4,
        fetch_top_k: int = _DEFAULT_FETCH_TOP_K,
        deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.search_provider = search_provider
        self.fetcher = fetcher
        self.encryption = encryption
        self.planner = planner
        self.evidence_extractor = evidence_extractor or EvidenceExtractor()
        self.synthesizer = synthesizer
        self.fallback = fallback
        self.max_rounds = max_rounds
        self.max_queries = max_queries
        self.min_trusted_leads = min_trusted_leads
        self.fetch_top_k = max(0, fetch_top_k)
        self.deadline_seconds = deadline_seconds
        self._search_concurrency = max(1, search_concurrency)
        self._fetch_concurrency = max(1, fetch_concurrency)

    async def run(
        self,
        query: str,
        kind: str = "text",
        limit: int = 10,
        progress: asyncio.Queue | None = None,
    ) -> AgentResult:
        if self.planner is None or self.synthesizer is None:
            await self._emit(progress, "degraded", "未配置 LLM，使用基础搜索")
            return self._run_fallback(query, limit)

        await self._emit(progress, "planning", "正在规划搜索式…")
        session_id = self._create_session(query, kind)
        return await self._run_query(query, session_id, limit, progress)

    async def follow_up(
        self,
        session_id: str,
        query: str,
        limit: int = 10,
        progress: asyncio.Queue | None = None,
    ) -> AgentResult:
        if self.planner is None or self.synthesizer is None:
            await self._emit(progress, "degraded", "未配置 LLM，使用基础搜索")
            return self._run_fallback(query, limit)
        if not self._session_exists(session_id):
            raise LookupError(f"BdSearchSession not found: {session_id}")
        return await self._run_query(query, session_id, limit, progress)

    # --- internals ---

    async def _emit(
        self, progress: asyncio.Queue | None, stage: str, message: str, **extra: object
    ) -> None:
        if progress is not None:
            await progress.put({"stage": stage, "message": message, **extra})

    @staticmethod
    def _remaining(deadline: float) -> float:
        return deadline - time.monotonic()

    async def _bounded(
        self,
        factory,
        *,
        timeout: float,
        progress: asyncio.Queue | None,
        label: str,
    ):
        """给单个阶段加超时：预算不足或超时都返回 None，由调用方降级，绝不拖住整轮。"""
        if timeout <= 0:
            await self._emit(progress, "budget", f"剩余预算不足，跳过{label}")
            return None
        try:
            return await asyncio.wait_for(factory(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._emit(progress, "timeout", f"{label}超时，使用已有证据继续")
            return None

    async def _synthesis_step(
        self,
        query: str,
        chunks: list[RankedChunk],
        *,
        progress: asyncio.Queue | None,
        deadline: float,
    ) -> tuple[SynthesisResult | None, str | None]:
        """执行线索综合，返回 ``(结果, 失败原因)``。

        为什么不复用 `_bounded`：那边只丢一条通用消息，而这里的失败原因要一路传到
        前端空态上。必须让使用者分得清「确实没搜到线索」和「综合这一步失败了」——
        前者可以换关键词，后者要去看 AI 配置。
        """
        timeout = min(_SYNTHESIS_TIMEOUT_SECONDS, self._remaining(deadline))
        if timeout <= 0:
            reason = "总时长预算已用尽，未执行线索综合"
            await self._emit(progress, "synthesis_failed", reason)
            return None, reason
        try:
            result = await asyncio.wait_for(
                self.synthesizer.synthesize(query, chunks),  # type: ignore[union-attr]
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            reason = f"线索综合超过 {timeout:.0f} 秒未返回"
        except ProviderError as error:
            detail = error.user_message or error.code
            reason = f"线索综合失败：{detail}（{error.code}）"
        except Exception as error:  # noqa: BLE001
            reason = f"线索综合失败：{type(error).__name__}"
        else:
            return result, None
        await self._emit(progress, "synthesis_failed", reason)
        return None, reason

    async def _run_query(
        self,
        query: str,
        session_id: str,
        limit: int,
        progress: asyncio.Queue | None,
    ) -> AgentResult:
        # 全局预算：交互场景以「最快给出可用结果」为目标，到点就用已攒线索收尾。
        deadline = time.monotonic() + self.deadline_seconds
        planned = await self._bounded(
            lambda: self.planner.plan(query),  # type: ignore[union-attr]
            timeout=min(_PLAN_TIMEOUT_SECONDS, self._remaining(deadline)),
            progress=progress,
            label="搜索式规划",
        )
        queries: list[str] = planned or [query]
        await self._emit(progress, "planned", f"规划出 {len(queries)} 条搜索式")
        seen_queries: set[str] = set()
        seen_leads: set[str] = self._load_seen_leads(session_id)
        docs_by_url: dict[str, EvidenceDoc] = {}
        accumulated: list[BdLead] = []
        degraded_reason: str | None = None

        for _round in range(self.max_rounds):
            if self._remaining(deadline) <= 0:
                await self._emit(progress, "budget", "总时长预算已用尽，返回已有线索")
                break
            await self._emit(progress, "searching", f"第 {_round + 1} 轮搜索中…")
            round_docs = await self._bounded(
                lambda: self._search_and_fetch(queries, limit, seen_queries),
                timeout=min(_RETRIEVE_TIMEOUT_SECONDS, self._remaining(deadline)),
                progress=progress,
                label="搜索抓取",
            ) or []
            for doc in round_docs:
                docs_by_url.setdefault(doc.source_url, doc)

            await self._emit(progress, "fetched", f"已抓取 {len(docs_by_url)} 个页面")
            chunks = await self.evidence_extractor.extract(query, list(docs_by_url.values()))
            # 标题按来源缓存：模型没给出公司名时，回捞要靠它（招聘页的雇主名常在标题里）。
            chunk_titles = {
                chunk.source_url: chunk.title for chunk in chunks if chunk.title
            }
            await self._emit(progress, "ranking", f"重排出 {len(chunks)} 个证据片段")
            # 综合走思考模型，可能安静地跑一到两分钟；先发一条进度，避免界面看起来卡死。
            await self._emit(progress, "synthesizing", f"正在综合 {len(chunks)} 个证据片段为线索…")
            synthesis, failure = await self._synthesis_step(
                query, chunks, progress=progress, deadline=deadline
            )
            if synthesis is None:
                # 本轮没有结论：记下原因后用已攒到的线索收尾，不再开启新一轮（否则会继续烧预算）。
                degraded_reason = failure or "线索综合未返回结果"
                break

            # 跨轮去重：同一「公司+岗位」只保留首次命中，保证统计的是去重后的可信结果。
            fresh = [
                item
                for item in synthesis.leads
                if self._dedup_key(item.company, item.job_title) not in seen_leads
            ]
            for item in fresh:
                seen_leads.add(self._dedup_key(item.company, item.job_title))
            synthesis.leads = fresh
            await self._emit(progress, "synthesized", f"综合出 {len(fresh)} 条线索")

            leads = self._persist(session_id, query, synthesis, chunk_titles)
            accumulated.extend(leads)
            if leads:
                # 增量推送：前端拿到本轮线索即可先渲染，不必等整批跑完。
                await self._emit(
                    progress, "leads", f"第 {_round + 1} 轮新增 {len(leads)} 条线索", leads=leads
                )

            if not self._should_continue(synthesis, _round, queries, accumulated):
                await self._emit(progress, "done", "完成")
                return AgentResult(
                    session_id=session_id,
                    leads=self._rank(accumulated, limit),
                    degraded_reason=degraded_reason,
                )
            queries = synthesis.follow_up_queries or []

        await self._emit(progress, "done", "完成")
        return AgentResult(
            session_id=session_id,
            leads=self._rank(accumulated, limit),
            degraded_reason=degraded_reason,
        )

    async def _search_and_fetch(
        self,
        queries: list[str],
        limit: int,
        seen_queries: set[str],
    ) -> list[EvidenceDoc]:
        # 一次性确定本轮要执行的查询式并扣减预算，避免与并行任务共享可变状态。
        planned: list[str] = []
        for query in queries:
            if query in seen_queries or len(seen_queries) >= self.max_queries:
                continue
            seen_queries.add(query)
            planned.append(query)
        if not planned:
            return []

        # 并行搜索所有查询式（同步 provider 放入线程池，信号量限流）。
        search_sem = asyncio.Semaphore(self._search_concurrency)

        async def run_search(query: str) -> list[WebSearchResult]:
            async with search_sem:
                return await asyncio.to_thread(self.search_provider.search, query, limit)

        search_outcomes = await asyncio.gather(
            *(run_search(q) for q in planned), return_exceptions=True
        )

        results: list[WebSearchResult] = []
        for outcome in search_outcomes:
            if isinstance(outcome, BaseException):
                continue
            results.extend(outcome)

        # 只抓「来源质量最高」的前 K 个页面的正文，其余直接复用搜索摘要：
        # 全量抓取是这一轮最大的时间黑洞，而低质量来源本来就会被下游丢弃。
        fetch_urls: set[str] = set()
        for result in sorted(results, key=lambda item: -source_quality(item.url)):
            if result.raw_content or result.url in fetch_urls:
                continue
            fetch_urls.add(result.url)
            if len(fetch_urls) >= self.fetch_top_k:
                break

        # 并行抓取被选中的 URL；单个页面超时即改用搜索摘要，不拖累本轮其余页面。
        fetch_sem = asyncio.Semaphore(self._fetch_concurrency)

        async def fetch_content(result: WebSearchResult) -> str | None:
            if result.raw_content:
                return result.raw_content
            if result.url not in fetch_urls:
                return result.snippet
            async with fetch_sem:
                try:
                    content = await asyncio.wait_for(
                        self.fetcher.fetch(result.url),
                        timeout=_FETCH_PER_URL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    content = None
            return content or result.snippet

        contents = await asyncio.gather(
            *(fetch_content(result) for result in results), return_exceptions=True
        )

        docs: list[EvidenceDoc] = []
        for result, content in zip(results, contents):
            if isinstance(content, BaseException) or not content:
                content = result.snippet
            docs.append(
                EvidenceDoc(source_url=result.url, title=result.title, content=content)
            )
        return docs

    def _load_seen_leads(self, session_id: str) -> set[str]:
        """同一会话内已持久化的去重键，跨轮、跨追问复用，避免重复出现。

        从 ``synthesized_json``（明文）读取公司与岗位，与 ``_persist`` 加密前的
        内容一致，因此无需额外解密即可还原去重键。
        """
        keys: set[str] = set()
        with self.session_factory() as session:
            rows = session.execute(
                select(BdLead.synthesized_json).where(BdLead.session_id == session_id)
            ).all()
        for (synthesized,) in rows:
            if not synthesized:
                continue
            company = synthesized.get("company")
            job_title = synthesized.get("job_title")
            if company or job_title:
                keys.add(self._dedup_key(company, job_title))
        return keys

    def _should_continue(
        self,
        synthesis: SynthesisResult,
        round_index: int,
        queries: list[str],
        accumulated: list[BdLead],
    ) -> bool:
        if round_index >= self.max_rounds - 1:
            return False
        trusted = sum(1 for lead in accumulated if self._is_trusted(lead))
        if trusted >= self.min_trusted_leads:
            return False
        if not synthesis.needs_more_search and not synthesis.follow_up_queries:
            return False
        return bool(synthesis.follow_up_queries)

    @staticmethod
    def _dedup_key(company: str | None, job_title: str | None) -> str:
        company = _normalize_company(company or "")
        job = (job_title or "").strip().casefold()
        return f"{company}\u0000{job}"

    @staticmethod
    def _is_trusted(lead: BdLead) -> bool:
        return lead.confidence is None or lead.confidence >= 0.6

    @staticmethod
    def _rank(leads: list[BdLead], limit: int) -> list[BdLead]:
        def sort_key(lead: BdLead):
            confidence = lead.confidence if lead.confidence is not None else -1.0
            evidence_count = len(lead.evidence) if lead.evidence else 0
            has_posted = 1 if lead.posted_time else 0
            return (confidence, evidence_count, has_posted)

        return sorted(leads, key=sort_key, reverse=True)[:limit]

    def _persist(
        self,
        session_id: str,
        query: str,
        synthesis: SynthesisResult,
        chunk_titles: dict[str, str] | None = None,
    ) -> list[BdLead]:
        """落库。模型没给出公司名时先做一次确定性回捞，再退回「未识别公司」。

        为什么不再直接写「未识别公司」：兜底链路（``BdSearchService``）在同样情况下会退回
        搜索标题（``info.company or r.title``），Agent 链路用的却是最严的一套口径——同一件事
        两条链路给出两种结果，而且把「抽取失败」记成了看起来像「页面确实没写公司」的终态。
        """
        titles = chunk_titles or {}
        leads: list[BdLead] = []
        with self.session_factory() as session:
            for item in synthesis.leads:
                source_url = item.evidence[0].source_url if item.evidence else None
                company = (
                    item.company
                    or _recover_company_name(
                        title=titles.get(source_url) if source_url else None,
                        url=source_url,
                        snippet=item.summary,
                    )
                    or "未识别公司"
                )
                lead = BdLead(
                    source="agent",
                    query=query,
                    company_name=self.encryption.encrypt(company),
                    job_title=(
                        self.encryption.encrypt(item.job_title)
                        if item.job_title
                        else None
                    ),
                    raw_snippet=item.summary,
                    url=(item.evidence[0].source_url if item.evidence else None),
                    status="新线索",
                    confidence=item.confidence,
                    is_hiring=item.is_hiring,
                    session_id=session_id,
                    synthesized_json=item.model_dump(),
                    posted_time=item.posted_time,
                    salary_range=item.salary_range,
                    level=item.level,
                    requirements=item.requirements or None,
                )
                session.add(lead)
                session.flush()
                for evidence in item.evidence:
                    lead.evidence.append(
                        BdEvidence(
                            claim=evidence.claim,
                            quote=evidence.quote,
                            source_url=evidence.source_url,
                            relevance_score=None,
                        )
                    )
                leads.append(lead)
            session.commit()
        return leads

    def _create_session(self, query: str, kind: str) -> str:
        with self.session_factory() as session:
            search_session = BdSearchSession(query=query, kind=kind)
            session.add(search_session)
            session.commit()
            return search_session.id

    def _session_exists(self, session_id: str) -> bool:
        with self.session_factory() as session:
            return session.get(BdSearchSession, session_id) is not None

    def _run_fallback(self, query: str, limit: int) -> AgentResult:
        reason = "未配置可用的 AI 供应商，已降级为基础搜索"
        if self.fallback is None:
            return AgentResult(session_id="", leads=[], degraded_reason=reason)
        leads = self.fallback.search_leads(query, limit)
        return AgentResult(session_id="", leads=leads, degraded_reason=reason)
