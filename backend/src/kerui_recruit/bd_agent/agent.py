from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.bd_agent.evidence import EvidenceDoc, EvidenceExtractor, RankedChunk
from kerui_recruit.bd_agent.fetcher import WebFetcher
from kerui_recruit.bd_agent.planner import QueryPlanner
from kerui_recruit.bd_agent.synthesis import SynthesisGenerator, SynthesisResult
from kerui_recruit.bd_search.service import BdSearchService, WebSearchProvider
from kerui_recruit.db.models import BdEvidence, BdLead, BdSearchSession
from kerui_recruit.encryption.service import EncryptionService


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


@dataclass(frozen=True, slots=True)
class AgentResult:
    session_id: str
    leads: list[BdLead]


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
        self, progress: asyncio.Queue | None, stage: str, message: str
    ) -> None:
        if progress is not None:
            await progress.put({"stage": stage, "message": message})

    async def _run_query(
        self,
        query: str,
        session_id: str,
        limit: int,
        progress: asyncio.Queue | None,
    ) -> AgentResult:
        queries = await self.planner.plan(query)  # type: ignore[union-attr]
        await self._emit(progress, "planned", f"规划出 {len(queries)} 条搜索式")
        seen_queries: set[str] = set()
        seen_leads: set[str] = self._load_seen_leads(session_id)
        docs_by_url: dict[str, EvidenceDoc] = {}
        accumulated: list[BdLead] = []

        for _round in range(self.max_rounds):
            await self._emit(progress, "searching", f"第 {_round + 1} 轮搜索中…")
            round_docs = await self._search_and_fetch(queries, limit, seen_queries)
            for doc in round_docs:
                docs_by_url.setdefault(doc.source_url, doc)

            await self._emit(progress, "fetched", f"已抓取 {len(docs_by_url)} 个页面")
            chunks = await self.evidence_extractor.extract(query, list(docs_by_url.values()))
            await self._emit(progress, "ranking", f"重排出 {len(chunks)} 个证据片段")
            synthesis = await self.synthesizer.synthesize(query, chunks)  # type: ignore[union-attr]

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

            leads = self._persist(session_id, query, synthesis)
            accumulated.extend(leads)

            if not self._should_continue(synthesis, _round, queries, accumulated):
                await self._emit(progress, "done", "完成")
                return AgentResult(
                    session_id=session_id, leads=self._rank(accumulated, limit)
                )
            queries = synthesis.follow_up_queries or []

        await self._emit(progress, "done", "完成")
        return AgentResult(session_id=session_id, leads=self._rank(accumulated, limit))

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

        # 并行抓取缺失正文的 URL；有 raw_content 的直接复用。
        fetch_sem = asyncio.Semaphore(self._fetch_concurrency)

        async def fetch_content(result: WebSearchResult) -> str | None:
            if result.raw_content:
                return result.raw_content
            async with fetch_sem:
                content = await self.fetcher.fetch(result.url)
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
    ) -> list[BdLead]:
        leads: list[BdLead] = []
        with self.session_factory() as session:
            for item in synthesis.leads:
                company = item.company or "未识别公司"
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
        if self.fallback is None:
            return AgentResult(session_id="", leads=[])
        leads = self.fallback.search_leads(query, limit)
        return AgentResult(session_id="", leads=leads)
