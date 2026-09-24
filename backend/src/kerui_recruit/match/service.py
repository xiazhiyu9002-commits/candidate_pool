from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
import json
from math import ceil
import time
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, Jd, JdRevision, MatchResult, MatchRun, ResumeDocument, ResumeRevision
from kerui_recruit.direction.policy import confirmed_multi_directions
from kerui_recruit.match.candidate_view import build_candidate_view
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.match.keywords import (
    build_candidate_biz_terms,
    build_candidate_query_text,
    build_candidate_tech_terms,
    build_jd_biz_terms,
    build_jd_query_text,
    build_jd_tech_terms,
)
from kerui_recruit.match.recall import merge_recall_lanes
from kerui_recruit.search.contracts import (
    CandidateFilters,
    EvidenceChunk,
    SearchHit,
    SearchPage,
    school_levels_at_least,
)
from kerui_recruit.search.degrees import degrees_at_least, normalize_degree
from kerui_recruit.search.industry import normalize_industries
from kerui_recruit.search.lexicon import tokenize_lexical_text
from kerui_recruit.search.query import has_skill, normalize_skill
from kerui_recruit.search.live import projection_is_current
from kerui_recruit.search.service import HybridSearchService, _blocking


class MatchEligibilityError(ValueError):
    """Current business entities do not permit starting or recording a match.

    带显式 ``code`` 是给任务链用的：资格判定是**确定性业务结论**，重试同一个 JD/候选人
    不会变合法。没有码时 worker 只能归到 `E_TASK_HANDLER` 并照常重试满 max_attempts
    （实测 `.dev-data` 有 10 条这样的死信，每条白重试 5 次）。
    """

    code = "E_ENTITY_NOT_ELIGIBLE"


class ReverseMatchUnavailableError(RuntimeError):
    """A reverse-match dependency is unavailable, rather than no jobs matching."""


@dataclass(frozen=True, slots=True)
class MatchDecision:
    passed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchScore:
    candidate_id: str
    total: float
    breakdown: dict[str, float]
    reason: str = ""
    matched_skills: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    eligibility: str = "eligible"  # "eligible" | "pending"（方向待核等缺证据，不静默排除）
    match_tier: str = "needs_review"  # "recommend" | "needs_review"
    direction_reason: str = ""
    # 业务方向是否一致：True 命中 / False 不命中 / None 任一侧缺失。
    # 只参与排序分层（见 merge_by_business），不参与硬淘汰。
    business_match: bool | None = None


@dataclass(frozen=True, slots=True)
class RecordedRun:
    run_id: str
    result_ids: dict[str, str]  # candidate_id -> MatchResult.id


@dataclass(frozen=True, slots=True)
class ReverseMatchRecord:
    jd_id: str
    revision_id: str
    company: str
    title: str
    hit: SearchHit
    score: MatchScore | None = None


@dataclass(frozen=True, slots=True)
class _JdContext:
    revision_id: str
    jd_id: str
    source_text: str | None
    min_years: float | None
    highest_degree: str | None
    location: str | None
    parsed_data: dict | None


class MatchService:
    """JD-driven candidate matching, reusing the unified hybrid search service."""

    # 匹配总分最低阈值：低于该分数的结果视为不相关，直接丢弃（避免匹配结果过泛）。
    _MIN_SCORE = 0.4

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        search_service: HybridSearchService,
        jd_index: JdSearchIndex | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.search_service = search_service
        self.jd_index = jd_index
        self.reverse_search = (HybridSearchService(
            index=jd_index.index, embedding_provider=search_service.embedding_provider,
            reranker_provider=search_service.reranker_provider, search_timeout=search_service.search_timeout)
            if jd_index else None)

    async def match_jd(
        self,
        *,
        revision_id: str,
        candidates: CandidateFilters | None = None,
        limit: int = 20,
        mode: str = "hybrid",
    ) -> SearchPage:
        deadline = time.monotonic() + self.search_service.search_timeout
        if not await _blocking(self._jd_eligible, revision_id, deadline=deadline):
            return SearchPage(items=(), empty_reason="jd_not_eligible")
        revision = await _blocking(self._revision, revision_id, deadline=deadline)
        query_text = _query_text(revision)
        filters = _hard_filter(revision, candidates)
        # 语义检索（embedding/rerank）外部 API 抖动时，不得耗尽整个匹配预算：预留时间给
        # 资格校验与评分，这样即使向量/重排失败，也能用 FTS 召回结果继续完成匹配（优雅降级）。
        now = time.monotonic()
        search_deadline = max(now + 1.0, deadline - 2.0)
        # 双通道召回：同向通道（带方向窄化）+ 救援通道（取消方向窄化，救回方向待核/跨方向者）。
        same_page = await self.search_service.search(
            query_text, filters, limit=70, mode=mode, deadline=search_deadline)
        # 安全阀（方案 §2.3）：硬条件下推是 AND 关系，若把召回池清空则自动回退后再试一次，
        # 避免 AI 误判硬条件导致「一个候选都推不出来」的静默 0 结果。
        relaxed: tuple[str, ...] = ()
        if not same_page.items:
            pushdown = _hard_filter_pushdown(revision.parsed_data or {})
            if pushdown:
                relaxed = tuple(sorted(pushdown))
                filters = replace(filters, school_level=None, companies=(), evidence_terms=())
                same_page = await self.search_service.search(
                    query_text, filters, limit=70, mode=mode, deadline=search_deadline)
        if filters.career_specializations or filters.career_directions:
            rescue_page = await self.search_service.search(
                query_text, replace(filters, career_specializations=(), career_directions=()),
                limit=30, mode=mode, deadline=search_deadline)
            merged = merge_recall_lanes(
                list(same_page.items), list(rescue_page.items),
                key=lambda hit: hit.candidate_id, limit=100, rescue_slots=30)
            page = replace(same_page, items=tuple(merged),
                           degraded_reasons=tuple(dict.fromkeys((*same_page.degraded_reasons, *rescue_page.degraded_reasons))))
        else:
            page = same_page
        try:
            hits = await _blocking(self._eligible_hits, revision_id, page.items, deadline=deadline)
        except Exception:
            return SearchPage(items=(), empty_reason="service_error", degraded_reasons=(*page.degraded_reasons, "LIVE_VALIDATION_UNAVAILABLE"))
        # S4：为最终量级内的候选人定向补齐多片段证据包（只读，失败不影响匹配）。
        try:
            hits = await _blocking(self._enrich_evidence, hits, limit,
                                   _term_tokens(revision.parsed_data or {}), deadline=deadline)
        except Exception:
            pass
        try:
            scored = await _blocking(self._score_and_sort, revision, hits, limit, deadline=deadline)
        except Exception:
            # 优雅降级：仅在评分预算被语义检索耗尽时，退回召回结果（仍由资格检查过滤），
            # 避免外部 API 抖动导致整条匹配变成 0 结果。真正无召回时仍返回空。
            from kerui_recruit.match.policy import evaluate_pair
            data_map = self._candidate_parsed_data([hit.candidate_id for hit in page.items])
            fallback = [
                hit for hit in page.items
                if evaluate_pair(revision.parsed_data or {}, data_map.get(hit.candidate_id, {})).eligibility != "rejected"
            ][:limit]
            return replace(page, items=tuple(fallback), empty_reason=page.empty_reason if fallback else (page.empty_reason or "no_match"),
                           degraded_reasons=(*page.degraded_reasons, "SCORING_UNAVAILABLE"),
                           hard_filters=_constraint_summary(revision.parsed_data or {}), relaxed=relaxed)
        return replace(page, items=tuple(scored), empty_reason=page.empty_reason if scored else (page.empty_reason or "no_match"),
                       hard_filters=_constraint_summary(revision.parsed_data or {}), relaxed=relaxed)

    def score(self, revision_id: str, hit: SearchHit) -> MatchScore:
        """Produce a transparent, sub-scored match result for one candidate.

        Scores are kept in a consistent 0~1 scale: relevance (rerank), business
        (skill coverage + years). The raw RRF recall score is never added to
        0~1 sub-scores by nominal weight.
        """
        data = self._candidate_parsed_data([hit.candidate_id]).get(hit.candidate_id, {})
        return self._score_context(self._revision(revision_id), hit, data)

    def _score_context(self, revision: _JdContext, hit: SearchHit, candidate_data: dict | None = None) -> MatchScore:
        candidate_data = candidate_data or {}
        must_skills = _must_skills(revision)
        matched, missing = _skill_coverage(candidate_data, must_skills)
        skill_score = (len(matched) / len(must_skills)) if must_skills else 0.0
        year_score = 1.0 if _meets_years(revision, hit) else 0.0
        direction_score, direction_reason, direction_tier = _career_assessment(revision, candidate_data)
        business_score = _business_assessment(revision, candidate_data)
        industry_score = _industry_match(revision, candidate_data)

        from kerui_recruit.match.policy import evaluate_pair  # 延迟导入，避免 service<->policy 循环
        decision = evaluate_pair(revision.parsed_data or {}, candidate_data)
        duties = (revision.parsed_data or {}).get("core_duties") or []
        duty_score = (len(decision.duty_evidence) / len(duties)) if duties else None

        components = {
            "relevance": hit.rerank_score if hit.rerank_score is not None else 0.0,
            "skill_coverage": skill_score,
            "years": year_score,
        }
        if direction_score is not None:
            components["career"] = direction_score
        if business_score is not None:
            components["business"] = business_score
        if industry_score is not None:
            components["industry"] = industry_score
        if duty_score is not None:
            components["duty"] = duty_score
        # 优先项（PLUS）命中率：降级后的 skill / other_keyword 靠它继续影响排序而不淘汰人。
        if decision.preference is not None:
            components["preference"] = decision.preference

        # 职责证据为主排序因子（35%），技能 25%，项目/语义 20%，资历 10%，职业方向 15%，业务方向 10%。
        # duty/career/business/industry 仅在对应证据存在时加入权重，缺失时按其余组件归一。
        if hit.rerank_score is not None:
            weights = {"skill_coverage": 0.25, "relevance": 0.2, "years": 0.1}
            rerank_available = 1
        else:
            weights = {"skill_coverage": 0.3, "years": 0.15}
            rerank_available = 0
        if duty_score is not None:
            weights["duty"] = 0.35 if rerank_available else 0.4
        if direction_score is not None:
            weights["career"] = 0.15 if rerank_available else 0.2
        if business_score is not None:
            weights["business"] = 0.1
        if industry_score is not None:
            weights["industry"] = 0.05
        if decision.preference is not None:
            # 与 business 同量级：优先项应当能改变排序，但不能压过职责/技能/方向等主因子。
            weights["preference"] = _PREFERENCE_WEIGHT
        if not must_skills:
            weights.pop("skill_coverage", None)
        if revision.min_years is None:
            weights.pop("years", None)
        total_weight = sum(weights.values())
        total = round(sum(components[k] * w for k, w in weights.items()) / total_weight, 4) if total_weight else 0.0
        breakdown = {k: round(components[k], 4) for k in weights}
        breakdown["rerank_available"] = rerank_available
        breakdown["matched_skills"] = matched
        breakdown["missing_skills"] = missing
        breakdown["duty_evidence"] = list(decision.duty_evidence)
        # 业务方向一致与否只影响排序分层，不影响基础分；落库供前端标记与 AI 复核。
        breakdown["business_match"] = decision.business_match
        # 证据包（S4）：父画像 + 技术最相关片段 + 业务最相关片段，供前端展示与 AI 复核引用。
        evidence_pack = _evidence_pack(hit, revision.parsed_data or {}, candidate_data)
        if evidence_pack:
            breakdown["evidence_pack"] = evidence_pack

        # 分层：方向同向且有一项技能/职责证据 → recommend；其余 eligible → needs_review。
        has_evidence = bool(matched) or bool(decision.duty_evidence)
        match_tier = "recommend" if (direction_tier == "recommend" and has_evidence) else "needs_review"

        return MatchScore(
            candidate_id=hit.candidate_id,
            total=total,
            breakdown=breakdown,
            reason=_build_reason(revision, hit, matched, must_skills, total, rerank_available),
            matched_skills=tuple(matched),
            missing_skills=tuple(missing),
            eligibility=decision.eligibility,
            match_tier=match_tier,
            direction_reason=direction_reason,
            business_match=decision.business_match,
        )

    def _candidate_parsed_data(self, candidate_ids: list[str]) -> dict[str, dict]:
        if not candidate_ids:
            return {}
        with self.session_factory() as session:
            revisions = session.scalars(select(ResumeRevision).join(ResumeDocument).where(
                ResumeDocument.candidate_id.in_(list(candidate_ids)),
                ResumeRevision.is_current.is_(True),
                ResumeRevision.status == "READY")).all()
            grouped: dict[str, list[ResumeRevision]] = defaultdict(list)
            for rev in revisions:
                grouped[rev.document.candidate_id].append(rev)
            candidates = {c.id: c for c in session.scalars(select(Candidate).where(Candidate.id.in_(list(candidate_ids)))).all()}
            # 统一人选证据视图：硬字段取最近修订，技能/经历/项目跨修订合并。
            return {cid: build_candidate_view(revs, candidates.get(cid)) for cid, revs in grouped.items()}

    def _attach_evidence(self, hit: SearchHit, *, index=None, terms: set[str] | None = None) -> SearchHit:
        """按 revision 定向读回该实体的 chunk，补足「概况 + 子片段」证据包。

        混合模式的向量通道带相似度阈值（``VECTOR_FUSION_MIN_SIMILARITY``），会把大部分子片段
        滤掉，导致证据包只剩父片段。这里对最终结果量级内的实体做一次**只读**定向读取
        （LanceDB 按 revision_id 过滤），不新增 embedding / 重排调用。

        ``index`` 指定 chunk 所在索引：正向用候选人索引（默认），反向必须传岗位索引，
        否则会拿 JD 修订 ID 去查候选人索引、什么也读不到。
        ``terms`` 是对侧的技术 / 业务词 token（用于挑最相关的子片段）。

        概况只认**真实** parent 行；索引里确实没有 parent 时保留候选人的真实
        ``representative_kind``，不把 child 片段标成 parent。
        """
        index = index if index is not None else getattr(self.search_service, "index", None)
        getter = getattr(index, "get_revision_chunks", None)
        if getter is None:
            return hit
        rows = getter(hit.revision_id) or []
        extras = _related_chunks(rows, terms or set())
        parent = next((row for row in rows if _is_parent_row(row)), None)
        if parent is None:
            if not extras:
                return hit
            overview = EvidenceChunk(kind=getattr(hit, "representative_kind", "parent"), text=hit.content)
        else:
            overview = EvidenceChunk(kind="parent", text=" ".join(str(parent.get("keyword_text") or "").split()),
                                     chunk_id=str(parent.get("id") or "") or None,
                                     evidence_path=tuple(parent.get("evidence_path") or ()))
        return replace(hit, evidence=(overview, *extras))

    def _enrich_evidence(self, hits: list[SearchHit], limit: int, terms: set[str]) -> list[SearchHit]:
        """给召回靠前（且会在最终结果里）的候选人补齐证据包。"""
        cap = min(max(limit, 0), _EVIDENCE_ENRICH_LIMIT)
        return [self._attach_evidence(hit, terms=terms) if index < cap else hit
                for index, hit in enumerate(hits)]

    def _score_and_sort(self, revision: _JdContext, hits: list[SearchHit], limit: int) -> list[SearchHit]:
        from kerui_recruit.match.policy import evaluate_pair  # 延迟导入，避免 service<->policy 循环
        data_map = self._candidate_parsed_data([hit.candidate_id for hit in hits])
        scored: list[tuple[SearchHit, MatchScore]] = []
        for hit in hits:
            data = data_map.get(hit.candidate_id, {})
            # 统一资格判断：方向不一致 / 必需技能 0 命中 → 硬性剔除，不靠总分补偿。
            if evaluate_pair(revision.parsed_data or {}, data).eligibility == "rejected":
                continue
            score = self._score_context(revision, hit, data)
            if score.total >= self._MIN_SCORE:
                scored.append((hit, score))
        scored.sort(key=lambda pair: -pair[1].total)
        # 业务方向一致者置顶，但按 80/20 保留名额给其余高分者（方案 §4.2）。
        return merge_by_business([(hit, score.business_match) for hit, score in scored], limit)

    def record_run(self, *, revision_id: str, hits, mode: str = "hybrid") -> RecordedRun:
        """Persist an immutable match_run snapshot with sub-scored results."""
        result_ids: dict[str, str] = {}
        hits = list(hits)
        data_map = self._candidate_parsed_data([hit.candidate_id for hit in hits])
        with self.session_factory() as session:
            self._assert_record_eligible(session, {revision_id}, hits)
            context = self._context(session.get(JdRevision, revision_id))
            run = MatchRun(
                trigger="JD_MATCH",
                jd_revision_id=revision_id,
                query_text=_query_text(context),
                mode=mode,
            )
            session.add(run)
            session.flush()
            for hit in hits:
                score = self._score_context(context, hit, data_map.get(hit.candidate_id, {}))
                result = MatchResult(
                    run=run,
                    candidate_id=hit.candidate_id,
                    resume_revision_id=hit.revision_id,
                    jd_revision_id=revision_id,
                    total_score=score.total,
                    score_breakdown=score.breakdown,
                    reason=score.reason,
                    status="未处理",
                )
                session.add(result)
                session.flush()
                result_ids[hit.candidate_id] = result.id
            session.commit()
            return RecordedRun(run_id=run.id, result_ids=result_ids)

    async def match_and_record(
        self,
        *,
        revision_id: str,
        limit: int = 20,
        mode: str = "hybrid",
    ) -> RecordedRun:
        """Run a JD-driven match and persist the immutable run snapshot."""
        page = await self.match_jd(revision_id=revision_id, limit=limit, mode=mode)
        return self.record_run(revision_id=revision_id, hits=page.items, mode=mode)

    async def reverse_match_candidate(
        self,
        candidate_id: str,
        *,
        limit: int = 20,
        mode: str = "hybrid",
    ) -> list[ReverseMatchRecord]:
        """Embed one candidate representation and directly retrieve current job representations."""
        deadline = time.monotonic() + self.search_service.search_timeout
        try:
            candidate = await _blocking(self._candidate_representation, candidate_id, deadline=deadline)
            if candidate is None:
                return []
            if not await _blocking(self._has_eligible_jobs, deadline=deadline):
                return []
            if not await _blocking(self._has_eligible_jobs, True, deadline=deadline):
                raise ReverseMatchUnavailableError("JD index is awaiting synchronization")
            if self.reverse_search is None:
                raise ReverseMatchUnavailableError("JD index is not configured")
            if not await _blocking(self.jd_index.is_ready, deadline=deadline):
                raise ReverseMatchUnavailableError("JD index is empty or incompatible")
            # 职业方向：仅当候选人有可确认方向时才做召回窄化；未确认（空/OTHER/非法）不窄化。
            narrowing = await _blocking(
                self._career_narrowing_for, candidate_id, deadline=deadline)
            # 三种模式使用不同的查询文本：keyword 用结构化关键词（技术 + 业务），vector 用 AI 画像/向量文本，
            # hybrid 分别用关键词做 FTS、用向量文本做 embedding。
            candidate_hit, _, candidate_query = candidate
            if mode == "keyword":
                query = candidate_query
                vector_query = None
            elif mode == "vector":
                query = candidate_hit.vector_text or candidate_query
                vector_query = None
            else:
                query = candidate_query
                vector_query = candidate_hit.vector_text or candidate_query
            # 岗位级硬条件（当前为空集；年限窗口在 _reverse_records 里按各 JD 的 n 逐条判断）。
            # 救援通道只能清方向，必须继承其余硬条件——否则将来新增岗位级硬条件会被救援通道绕过。
            base_filters = CandidateFilters()
            page = await self.reverse_search.search(
                query, replace(base_filters, **narrowing), limit=70,
                mode=mode, vector_query=vector_query,
                deadline=deadline - min(.25, max(0., deadline - time.monotonic()) * .1))
            # 双通道召回：救援通道取消方向窄化，救回跨方向/方向待核岗位（按 jd_id 去重）。
            if narrowing:
                rescue_page = await self.reverse_search.search(
                    query, base_filters, limit=30,
                    mode=mode, vector_query=vector_query,
                    deadline=deadline - min(.25, max(0., deadline - time.monotonic()) * .1))
                merged = merge_recall_lanes(
                    list(page.items), list(rescue_page.items),
                    key=lambda hit: hit.candidate_id, limit=100, rescue_slots=30)
                page = replace(page, items=tuple(merged),
                               degraded_reasons=tuple(dict.fromkeys((*page.degraded_reasons, *rescue_page.degraded_reasons))))
            if not page.items and (page.degraded_reasons or page.empty_reason == "index_not_ready"):
                raise ReverseMatchUnavailableError("JD index is unavailable or search timed out")
            return await _blocking(self._reverse_records, candidate, page.items, limit, deadline=deadline)
        except TimeoutError as error:
            raise ReverseMatchUnavailableError("JD index search timed out") from error

    def _career_narrowing_for(self, candidate_id: str) -> dict:
        """候选人所在的方向召回窄化条件（供反向匹配使用）。"""
        return _career_narrowing(self._candidate_parsed_data([candidate_id]).get(candidate_id, {}))

    def _has_eligible_jobs(self, require_current_projection=False):
        with self.session_factory() as session:
            statement = select(JdRevision.id).join(Jd, Jd.id == JdRevision.jd_id).where(
                JdRevision.is_current.is_(True), JdRevision.status == "READY",
                Jd.status == "OPEN", Jd.deleted_at.is_(None))
            if require_current_projection:
                statement = statement.where(projection_is_current("jd", Jd.id))
            return session.scalar(statement.limit(1)) is not None

    def _candidate_representation(self, candidate_id):
        with self.session_factory() as session:
            candidate = session.scalar(select(Candidate).where(Candidate.id == candidate_id,
                                       projection_is_current("candidate", Candidate.id)))
            if candidate is None or candidate.deleted_at or candidate.status != "AVAILABLE":
                return None
            revisions = session.scalars(
                select(ResumeRevision).join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                .where(ResumeDocument.candidate_id == candidate_id, ResumeRevision.is_current.is_(True))
                .order_by(ResumeRevision.created_at.desc(), ResumeRevision.id.desc())
            ).all()
            if not revisions or any(revision.status != "READY" or not revision.raw_text for revision in revisions):
                return None
            content = "\n".join(revision.raw_text + "\n" + json.dumps(revision.parsed_data or {}, ensure_ascii=False)
                                for revision in revisions)
            # 统一人选证据视图：多份当前简历合并后的技能/经历/项目 + 最近修订的硬字段。
            parsed = build_candidate_view(revisions, candidate)
            from kerui_recruit.search.documents import build_candidate_document
            document = build_candidate_document(parsed, display_name=candidate.display_name)
            keyword_text = document["keyword_text"]
            vector_text = document["vector_text"]
            preferred = set()
            for revision in revisions:
                data = revision.parsed_data or {}
                preferred.update(data.get("preferred_locations") or ())
                if data.get("preferred_location"):
                    preferred.add(data["preferred_location"])
            hit = SearchHit(chunk_id=f"candidate:{candidate_id}", candidate_id=candidate_id,
                            revision_id=revisions[0].id, content=keyword_text or content, score=0., matched_channels=(),
                            total_years=float(candidate.total_years) if candidate.total_years is not None else parsed.get("total_years"),
                            highest_degree=candidate.highest_degree or parsed.get("highest_degree"), location=parsed.get("location"),
                            vector_text=vector_text)
            # 反向匹配的 FTS query 单独收敛为「技术 + 业务」两类（见 match/keywords.py）；
            # content 仍保留完整 keyword_text，供 EXCLUDE 证据与职责证据判断，避免削弱校验。
            return hit, preferred, build_candidate_query_text(parsed)

    def _reverse_records(self, candidate, job_hits, limit):
        source, preferred, _ = candidate
        current_candidate = self._candidate_representation(source.candidate_id)
        if current_candidate is None or current_candidate != candidate:
            return []
        source_data = self._candidate_parsed_data([source.candidate_id]).get(source.candidate_id, {})
        with self.session_factory() as session:
            rows = session.execute(
                select(JdRevision, Jd.company, Jd.title)
                .join(Jd, Jd.id == JdRevision.jd_id)
                .where(JdRevision.id.in_({hit.revision_id for hit in job_hits}),
                       Jd.status == "OPEN", Jd.deleted_at.is_(None), projection_is_current("jd", Jd.id),
                       JdRevision.is_current.is_(True), JdRevision.status == "READY")
            ).all()
            jobs = {(revision.jd_id, revision.id): (revision, company, title) for revision, company, title in rows}
            records = []
            for recalled in job_hits:
                row = jobs.get((recalled.candidate_id, recalled.revision_id))
                if row is None:
                    continue
                revision, company, title = row
                context = self._context(revision)
                filters = _hard_filter(context, None)
                # 年限硬窗口 [n-1, 2n]：反向匹配的 n 随各 JD 变化，只能在逐 JD 判断时施加；
                # 与岗位匹配人下推到检索层的窗口口径完全一致（见 _years_window）。
                if filters.min_years is not None and (source.total_years is None or source.total_years < filters.min_years):
                    continue
                if filters.max_years is not None and (source.total_years or 0) > filters.max_years:
                    continue
                if filters.degree_values() and normalize_degree(source.highest_degree) not in filters.degree_values():
                    continue
                if _is_plausible_location(context.location) and context.location not in {source.location, *preferred}:
                    continue
                exclusions = [requirement.get("value", "") for requirement in (context.parsed_data or {}).get("requirements", [])
                              if requirement.get("kind") == "EXCLUDE" and requirement.get("label") in ("技能", "skill")]
                if any(has_skill(source.content, skill) for skill in exclusions):
                    continue
                hit = replace(source, score=recalled.score, matched_channels=recalled.matched_channels,
                              rerank_score=recalled.rerank_score,
                              # 证据包取岗位侧的 chunk（recalled 属于岗位索引），与正向对称。
                              evidence=self._attach_evidence(
                                  recalled, index=getattr(self.reverse_search, "index", None),
                                  terms=_term_tokens(source_data)).evidence)
                score = self._score_context(context, hit, source_data)
                records.append(ReverseMatchRecord(revision.jd_id, revision.id, company, title, hit, score))
            eligible = [record for record in records
                        if record.score.eligibility != "rejected" and record.score.total >= self._MIN_SCORE]
            eligible.sort(key=lambda record: (-record.score.total, -record.hit.score, record.jd_id))
            return merge_by_business(
                [(record, record.score.business_match) for record in eligible], limit)

    def _jd_eligible(self, revision_id):
        with self.session_factory() as session:
            return session.scalar(select(JdRevision.id).join(Jd, Jd.id == JdRevision.jd_id).where(
                JdRevision.id == revision_id, JdRevision.is_current.is_(True), JdRevision.status == "READY",
                Jd.status == "OPEN", Jd.deleted_at.is_(None), projection_is_current("jd", Jd.id))) is not None

    def _eligible_hits(self, revision_id, hits):
        if not self._jd_eligible(revision_id) or not hits:
            return []
        with self.session_factory() as session:
            valid = set(session.execute(select(Candidate.id, ResumeRevision.id)
                .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
                .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                .where(Candidate.id.in_({hit.candidate_id for hit in hits}),
                       ResumeRevision.id.in_({hit.revision_id for hit in hits}),
                       Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE", projection_is_current("candidate", Candidate.id),
                       ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")).all())
            return [hit for hit in hits if (hit.candidate_id, hit.revision_id) in valid]

    def record_reverse_run(
        self,
        *,
        candidate_id: str,
        records: list[ReverseMatchRecord],
        mode: str = "hybrid",
    ) -> RecordedRun:
        """Persist a candidate-driven reverse match as a run snapshot."""
        result_ids: dict[str, str] = {}
        with self.session_factory() as session:
            if any(record.hit.candidate_id != candidate_id for record in records):
                raise MatchEligibilityError("Candidate is not eligible for these records")
            self._assert_record_eligible(session, {record.revision_id for record in records}, [record.hit for record in records])
            run = MatchRun(trigger="REVERSE_MATCH", jd_revision_id=None, mode=mode)
            session.add(run)
            session.flush()
            for record in records:
                score = self.score(record.revision_id, record.hit)
                result = MatchResult(
                    run=run,
                    candidate_id=candidate_id,
                    resume_revision_id=record.hit.revision_id,
                    jd_revision_id=record.revision_id,
                    total_score=score.total,
                    score_breakdown=score.breakdown,
                    reason=score.reason,
                    status="未处理",
                )
                session.add(result)
                session.flush()
                result_ids[record.revision_id] = result.id
            session.commit()
            return RecordedRun(run_id=run.id, result_ids=result_ids)

    def _revision(self, revision_id: str) -> _JdContext:
        with self.session_factory() as session:
            revision = session.get(JdRevision, revision_id)
            if revision is None:
                raise LookupError(f"Jd revision not found: {revision_id}")
            return self._context(revision)

    @staticmethod
    def _context(revision) -> _JdContext:
        return _JdContext(
            revision_id=revision.id,
            jd_id=revision.jd_id,
            source_text=revision.source_text,
            min_years=None if revision.min_years is None else float(revision.min_years),
            highest_degree=revision.highest_degree,
            location=revision.location,
            parsed_data=revision.parsed_data,
        )

    @staticmethod
    def _assert_record_eligible(session, revision_ids, hits):
        valid_jobs = set(session.scalars(select(JdRevision.id).join(Jd, Jd.id == JdRevision.jd_id).where(
            JdRevision.id.in_(revision_ids), JdRevision.is_current.is_(True), JdRevision.status == "READY",
            Jd.status == "OPEN", Jd.deleted_at.is_(None), projection_is_current("jd", Jd.id))).all())
        if valid_jobs != revision_ids:
            raise MatchEligibilityError("JD is not eligible for matching")
        valid_people = set(session.execute(select(Candidate.id, ResumeRevision.id)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.id.in_({hit.candidate_id for hit in hits}), Candidate.status == "AVAILABLE",
                   Candidate.deleted_at.is_(None), projection_is_current("candidate", Candidate.id), ResumeRevision.id.in_({hit.revision_id for hit in hits}),
                   ResumeRevision.status == "READY", ResumeRevision.is_current.is_(True))).all())
        if any((hit.candidate_id, hit.revision_id) not in valid_people for hit in hits):
            raise MatchEligibilityError("Candidate revision is not eligible for matching")


def _query_text(revision: _JdContext) -> str:
    """召回 query：只保留「技术 + 业务」两类关键词（见 match/keywords.py）。

    职责长句不再进 FTS——它属于自由文本，放在查询里只会稀释关键词通道；
    语义覆盖改由向量通道承担（JD 索引里每条职责/技能都是独立子 chunk）。
    """
    text = build_jd_query_text(revision.parsed_data or {})
    # 无技能/业务信息时兜底回退到岗位画像/原文，避免 query 为空退化为纯硬过滤。
    return text or ((revision.parsed_data or {}).get("summary") or revision.source_text or "")


# 明显不是城市的地点脏值（国家名、含编号/英文编码等），不应作为硬地点过滤条件。
# 这类值来自 AI 解析误判，若参与硬过滤会导致几乎所有候选人被误杀。
_NON_CITY_LOCATIONS = {"中国", "国内", "大陆", "中国大陆", "中国香港", "中国澳门", "中国台湾"}


def _is_plausible_location(location: str | None) -> bool:
    if not location:
        return False
    value = str(location).strip()
    if not value or value in _NON_CITY_LOCATIONS:
        return False
    # 含数字的「编号/编码」类值（如 "12 WMT"）不是城市。
    if any(ch.isdigit() for ch in value):
        return False
    return True


def _career_narrowing(parsed: dict) -> dict:
    """方向召回窄化条件：优先按细分，缺细分（或旧词表不可比）时回退大类；未确认方向不窄化。

    返回可直接 ``replace(filters, **narrowing)`` 的关键字；空字典表示不窄化。
    """
    career, specs, _ = confirmed_multi_directions(parsed)
    if specs:
        return {"career_specializations": specs}
    if career:
        return {"career_directions": career}
    return {}


# 业务方向一致的置顶比例：Top-N 中 80% 名额给业务方向一致者，其余 20% 留给总分更高但
# 业务方向不一致/缺失者。纯置顶在 limit 小于「一致人数」时等价于硬筛选，按比例可避免。
BUSINESS_QUOTA_RATIO = 0.8

_T = TypeVar("_T")


def merge_by_business(
    scored: Sequence[tuple[_T, bool | None]],
    limit: int,
    *,
    ratio: float = BUSINESS_QUOTA_RATIO,
) -> list[_T]:
    """业务方向一致者置顶，同时按比例给其余高分者保留名额（方案 §4.2）。

    入参 ``scored`` 需已按总分降序；``business_match`` 为 True 归入置顶组，
    False / None（任一侧缺业务方向）归入其余组且不惩罚。任一组不足配额时由另一组补满，
    保证结果条数只受候选总量限制、不因配比而减少。输出顺序：置顶组在前，组内保持总分降序。
    """
    if limit <= 0:
        return []
    preferred = [item for item, flag in scored if flag is True]
    others = [item for item, flag in scored if flag is not True]
    preferred_quota = min(len(preferred), ceil(limit * ratio))
    other_quota = min(len(others), limit - preferred_quota)
    if preferred_quota + other_quota < limit:
        preferred_quota = min(len(preferred), limit - other_quota)
    return preferred[:preferred_quota] + others[:other_quota]


# 证据包定向补齐的实体上限：只给最终结果量级内的候选/岗位补，避免整库读取。
_EVIDENCE_ENRICH_LIMIT = 50
# 每个证据包最多补几条子片段（父画像之外）：技术 / 业务各一条。
_EVIDENCE_EXTRA_CHUNKS = 2

# 优先项（PLUS）命中率在总分里的权重。与 business 同量级：优先项要能改变排序，
# 但不能压过职责证据、技能覆盖、职业方向这些主因子；调大它会让「优先」逐渐变成「必须」。
_PREFERENCE_WEIGHT = 0.1


def _is_parent_row(row: dict) -> bool:
    """判断索引行是不是父 chunk。

    候选人侧 ``kind`` 有语义（parent/profile_point/experience/project），岗位侧只写了
    ``chunk_type``（parent/child）而 ``kind`` 恒为默认值 ``parent``，因此以 ``chunk_type`` 为准。
    """
    chunk_type = str(row.get("chunk_type") or "").strip()
    if chunk_type:
        return chunk_type == "parent"
    return str(row.get("kind") or "parent") == "parent"


def _row_kind(row: dict) -> str:
    kind = str(row.get("kind") or "").strip()
    chunk_type = str(row.get("chunk_type") or "").strip()
    if not kind or (kind == "parent" and chunk_type == "child"):
        return chunk_type or "parent"
    return kind


def _term_tokens(*parsed_dicts: dict) -> set[str]:
    """把若干份 parsed_data 的技术 / 业务词展开成 token 集合（自动忽略不存在的字段）。"""
    tokens: set[str] = set()
    for parsed in parsed_dicts:
        for term in (*build_jd_tech_terms(parsed), *build_jd_biz_terms(parsed),
                     *build_candidate_tech_terms(parsed), *build_candidate_biz_terms(parsed)):
            tokens.update(tokenize_lexical_text(term))
    return tokens


def _related_chunks(rows: list[dict], terms: set[str],
                    limit: int = _EVIDENCE_EXTRA_CHUNKS) -> tuple[EvidenceChunk, ...]:
    """在实体自身的子片段里挑与 ``terms`` 最相关的若干条（token 命中数排序，文本去重）。"""
    scored: list[tuple[int, str, dict]] = []
    seen_texts: set[str] = set()
    for row in rows:
        if _is_parent_row(row):
            continue
        text = " ".join(str(row.get("keyword_text") or "").split())
        if not text or text in seen_texts:
            continue
        overlap = len(set(tokenize_lexical_text(text)) & terms) if terms else 0
        if terms and not overlap:
            continue
        seen_texts.add(text)
        scored.append((overlap, text, row))
    scored.sort(key=lambda item: -item[0])
    return tuple(EvidenceChunk(kind=_row_kind(row), text=text, score=float(overlap),
                               chunk_id=str(row.get("id") or "") or None,
                               sequence=row.get("sequence"),
                               evidence_path=tuple(row.get("evidence_path") or ()))
                 for overlap, text, row in scored[:limit])


def _years_window(min_years: float | None) -> tuple[float, float] | None:
    """年限硬窗口：``[n-1, 2n]``（``n`` 为岗位最低年限）；``n`` 缺失时不设窗口。

    口径（方案 §3.1）：
    - ``n <= 1``（一年经验）→ ``1 ~ 3``；
    - ``n >= 2`` → ``n-1 ~ 2n``（三年以上看 2~6，六年以上看 5~12）。

    ``n`` 缺失时**返回 None 而不是 ``0~3``**：本池 0~3 年只有十几人，套用会把 13 个没有年限
    字段的岗位直接清空。
    """
    if min_years is None:
        return None
    n = float(min_years)
    if n <= 1:
        return 1.0, 3.0
    return max(0.0, n - 1), n * 2


def _hard_filter(
    revision: _JdContext,
    provided: CandidateFilters | None,
) -> CandidateFilters:
    """JD 硬条件覆盖到 provided 之上，保留 provided 的其余字段（多地点/排除等）。"""
    base = provided if provided is not None else CandidateFilters()
    window = _years_window(revision.min_years)
    if window is not None:
        low, high = window
        base = replace(base, min_years=low, max_years=high)
    if revision.highest_degree:
        base = replace(base, highest_degree=normalize_degree(revision.highest_degree))
    # 只对可信的城市地点做硬过滤，脏值（国家/编号）不参与。
    if _is_plausible_location(revision.location):
        base = replace(base, location=revision.location)
    base = replace(base, **_hard_filter_pushdown(revision.parsed_data or {}))
    return replace(base, **_career_narrowing(revision.parsed_data or {}))


def _must_constraints(jd_parsed: dict) -> list[dict]:
    """筛出**具备淘汰力**的硬条件：`strength=MUST` 且带 `source_text` 原文依据。

    无原文依据的 MUST 已被 ``normalize_constraints`` 降级为 PLUS；这里再兜一层，
    防止绕过 normalize 的调用路径把没有依据的条件变成淘汰力。``SOFT_ONLY_KINDS``
    （skill / other_keyword）同理——存量库里可能还留着加固前写入的 `skill/MUST`，
    读侧同样不认，否则历史脏数据仍会把候选集清空。
    """
    from kerui_recruit.jd.profile_constraints import (
        SOFT_ONLY_KINDS,
        industry_requirement_is_explicit,
    )

    accepted: list[dict] = []
    for raw in jd_parsed.get("exact_constraints") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("strength") or "").upper() != "MUST":
            continue
        if str(raw.get("kind") or "").strip() in SOFT_ONLY_KINDS:
            continue
        if not str(raw.get("source_text") or "").strip():
            continue
        # 行业要求：没写明「必须 / 硬性 / 及以上」的不给淘汰力（与写侧同一判定）。
        if (str(raw.get("kind") or "").strip() == "industry"
                and not industry_requirement_is_explicit(str(raw.get("source_text") or ""))):
            continue
        if not [a for a in (raw.get("alternatives") or []) if str(a).strip()]:
            continue
        accepted.append(raw)
    return accepted


def _hard_filter_pushdown(jd_parsed: dict) -> dict:
    """把 AI 解析出的 MUST 硬条件下推到检索层（方案 §2）。

    kind 映射：

    - ``school_level``    -> ``CandidateFilters.school_level``（索引列 ``school_tags``，
      已含 985 / 211 / 双一流 的层级重叠关系）
    - ``degree``          -> ``CandidateFilters.highest_degree``（学历门槛，索引列 ``highest_degree``；
      语义是「最低学历层级」，与 ``search/degrees.degrees_at_least`` 一致）
    - ``company_history`` -> ``CandidateFilters.companies``（多值 OR；索引列 ``company_text``
      覆盖 current_company 与全部工作经历）
    - ``industry`` -> ``CandidateFilters.evidence_terms``（正文 ``body_index_text`` 子串证据匹配）。
      ``skill`` / ``other_keyword`` 已归入 ``SOFT_ONLY_KINDS``，不再具备淘汰力，因此不下推。

    下推是 AND 关系，会直接缩小候选集，调用方**必须**配套「池子被清空则回退」的安全阀。
    """
    school: list[str] = []
    degrees: list[str] = []
    companies: list[str] = []
    evidence: list[str] = []
    for raw in _must_constraints(jd_parsed):
        kind = str(raw.get("kind") or "").strip()
        alternatives = [str(a).strip() for a in (raw.get("alternatives") or []) if str(a).strip()]
        if kind == "school_level":
            school.extend(alternatives)
        elif kind == "degree":
            degrees.extend(alternatives)
        elif kind == "company_history":
            companies.extend(alternatives)
        elif kind == "industry":
            evidence.extend(alternatives)

    result: dict = {}
    if school:
        # 约束语义是 OR（任一满足即可），而 school_level 是单值「最低等级」，
        # 故取**最宽松**的选项：其展开集合最大（985 -> {985}，211 -> {211,985}，
        # 双一流 -> {双一流,211,985}）。取最严格的那个会把 OR 误变成 AND。
        result["school_level"] = max(school, key=lambda level: len(school_levels_at_least(level)))
    if degrees:
        # 学历同为「最低层级」语义：越低展开集合越大（本科 -> {本科,硕士,博士}），
        # 因此多值 OR 时同样取最宽松（最低）的那一档。
        normalized = [value for value in (normalize_degree(d) for d in degrees) if value]
        if normalized:
            result["highest_degree"] = max(normalized, key=lambda d: len(degrees_at_least(d)))
    if companies:
        result["companies"] = tuple(dict.fromkeys(companies))
    if evidence:
        result["evidence_terms"] = tuple(dict.fromkeys(evidence))
    return result


def _constraint_summary(jd_parsed: dict) -> tuple[dict, ...]:
    """对外暴露的硬条件摘要（kind / alternatives / 原文依据），供匹配结果页明示。"""
    return tuple(
        {
            "kind": str(raw.get("kind") or ""),
            "alternatives": [str(a) for a in (raw.get("alternatives") or []) if str(a).strip()],
            "source_text": str(raw.get("source_text") or ""),
        }
        for raw in _must_constraints(jd_parsed)
    )


# 泛词黑名单：软技能/纯能力描述，命中无岗位辨识度，不计入「必备技能覆盖」。
# 只降「跨岗位通用」的词，不放任何岗位专属技术/职能词。
_GENERIC_SKILLS = {
    # 软技能 / 通用能力（覆盖互联网/AI/金融等行业的通用软技能）
    "项目管理", "风险管理", "行业分析", "商业谈判", "需求规划", "需求分析",
    "英语", "普通话", "沟通", "沟通能力", "团队协作", "团队合作", "跨部门协调",
    "问题解决", "领导力", "时间管理", "抗压能力", "学习能力", "逻辑思维",
    "文档撰写", "技术分享", "代码评审", "汇报", "培训", "辅导", "带教",
    "客户沟通", "商务沟通", "协调", "组织能力", "规划能力", "主动性", "责任心",
    # 纯工程能力描述词（跨技术岗通用，非具体技术、非岗位专精）
    "系统架构设计", "架构设计", "高并发", "高可用", "可扩展性设计", "性能优化",
    "性能调优", "代码质量", "稳定性", "容灾", "高可靠",
}


def _must_skills(revision: _JdContext) -> list[str]:
    parsed = revision.parsed_data or {}
    # 只取 required_skills（单一技能词）；requirements 里的 MUST 技能多为长短语/重复，不再纳入硬条件。
    skills = [normalize_skill(s) for s in parsed.get("required_skills", [])]
    # 去重保序 + 过滤泛词 + 上限（与 match/policy._MAX_MUST_SKILLS 一致）。
    seen: set[str] = set()
    result: list[str] = []
    for skill in skills:
        key = skill.casefold()
        if skill and key not in seen and key not in _GENERIC_SKILLS:
            seen.add(key)
            result.append(skill)
    return result[:5]


def _skill_text(candidate_data: dict) -> str:
    parts: list[str] = []
    for key in ("skills",):
        value = candidate_data.get(key) or []
        if isinstance(value, str):
            parts.append(value)
        else:
            parts.extend(str(v) for v in value if v)
    for exp in candidate_data.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("title") or ""))
            parts.append(str(exp.get("summary") or ""))
    for proj in candidate_data.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("tech_stack") or ""))
            parts.append(str(proj.get("summary") or ""))
    return " ".join(p for p in parts if p)


# token 级停用词：技能拆词后过滤，避免「采购管理」因「管理」这类泛 token 误命中。
# 只放「动作/能力/连接」等泛词，不放任何核心名词/技术词（Java/采购/数据库/Avaloq…）。
_SKILL_STOPWORDS = {
    # 英文连接词/泛词
    "and", "or", "the", "of", "for", "with", "within", "a", "an", "to", "in", "on",
    "using", "based", "etc", "management", "design", "analysis", "optimization",
    "development", "experience", "skills", "skill", "knowledge", "related",
    "technical", "engineering", "framework", "frameworks", "integration",
    "solution", "solutions", "process", "processes", "ability", "abilities",
    "strong", "core", "platform", "system", "systems", "architecture",
    # 中文连接词/动作能力后缀词
    "相关", "等", "及",
    "管理", "设计", "分析", "优化", "评估", "跟踪", "执行", "规划", "协调",
    "沟通", "协作", "解决", "撰写", "分享", "评审", "汇报", "培训", "辅导", "带教",
    "谈判", "学习", "思维", "主动", "责任", "组织", "能力", "经验", "熟悉", "掌握",
    "具备", "了解", "使用", "搭建", "建设", "维护", "支持", "推进", "落地", "推动",
    "系统", "平台", "架构", "开发", "研发", "技术", "业务", "方案", "流程", "体系",
    "方法论", "实践",
}


# OR 连接词：技能中出现表示「多选一」，应任一命中而非全部命中。
# 注意：「and」「及」是 AND 连接词，不在此列；它们由 _SKILL_STOPWORDS 过滤后按 all 语义匹配。
_OR_CONNECTORS = {"或", "or", "/", "、", ",", "，", "；"}


def _skill_hit(candidate_tokens: set[str], skill: str) -> bool:
    """技能 token 级匹配：拆词后过滤停用词与连接词。

    - 含 OR 连接词（或/及/or/and/、/ 等）的「多选一」技能，任一核心 token 命中即可；
    - 复合技术名（如「Spring AI」「Spring Cloud」）要求全部核心 token 命中，
      避免「Spring AI」仅因命中「spring」而误判为覆盖。
    """
    raw = tokenize_lexical_text(skill)
    tokens = [t for t in raw if t not in _SKILL_STOPWORDS and t not in _OR_CONNECTORS]
    if not tokens:
        return False
    if any(t in _OR_CONNECTORS for t in raw):
        return any(t in candidate_tokens for t in tokens)
    return all(t in candidate_tokens for t in tokens)


def _skill_coverage(candidate_data: dict, must_skills: list[str]) -> tuple[list[str], list[str]]:
    if not must_skills:
        return [], []
    text = _skill_text(candidate_data)
    candidate_tokens = set(tokenize_lexical_text(text))
    matched = [skill for skill in must_skills if _skill_hit(candidate_tokens, skill)]
    return matched, [skill for skill in must_skills if skill not in matched]


# 证据片段在 breakdown / 前端悬停 / AI prompt 里的展示长度上限（截断避免长画像淹没提示）。
_EVIDENCE_TEXT_LIMIT = 240


def _evidence_pack(hit: SearchHit, jd_parsed: dict, candidate_data: dict) -> dict[str, str]:
    """把召回到的多条片段按「概况 / 技术最相关 / 业务最相关」分类（方案 S4）。

    - ``overview``：优先取**真实** ``kind=parent`` 的片段；索引里确实没有 parent 时，
      取贡献最高的片段并保留其原始 kind，绝不把非 parent 片段伪装成 parent；
    - ``tech``：其余片段里命中技术词最多的一条；
    - ``business``：其余片段里命中业务词最多的一条。

    技术词与业务词都取自结构化字段（``match/keywords.py``：JD 的 required_skills/技能类
    requirement + 候选人 skills/tech_stack；业务方向标签 + 行业/项目类型），**不新增模型调用**。
    两个方向共用：片段可能来自候选人索引（JD 找人）或岗位索引（人找 JD），
    因此匹配词表取两侧并集，语义是「这段证据提到了哪些技术 / 业务词」。

    搜索侧不区分技术/业务，另用 overview/primary/complementary（见 search/service.py）。
    """
    evidence = tuple(getattr(hit, "evidence", ()) or ())
    if not evidence:
        return {}
    tech_tokens = {token for term in (*build_jd_tech_terms(jd_parsed), *build_candidate_tech_terms(candidate_data))
                   for token in tokenize_lexical_text(term)}
    biz_tokens = {token for term in (*build_jd_biz_terms(jd_parsed), *build_candidate_biz_terms(candidate_data))
                  for token in tokenize_lexical_text(term)}
    def clip(text: str) -> str:
        return text if len(text) <= _EVIDENCE_TEXT_LIMIT else text[:_EVIDENCE_TEXT_LIMIT] + "…"

    overview = next((chunk for chunk in evidence if chunk.kind == "parent"), None)
    if overview is None:
        overview = max(evidence, key=lambda chunk: max(
            (signal.reciprocal_rank for signal in chunk.signals), default=0.0))
    pack = {"overview": clip(overview.text)}
    best: dict[str, tuple[int, str]] = {}
    for chunk in evidence:
        if chunk is overview:
            continue
        tokens = set(tokenize_lexical_text(chunk.text))
        for key, terms in (("tech", tech_tokens), ("business", biz_tokens)):
            overlap = len(tokens & terms)
            if overlap and (key not in best or overlap > best[key][0]):
                best[key] = (overlap, clip(chunk.text))
    for key in ("tech", "business"):
        if key in best:
            pack[key] = best[key][1]
    return pack


def _meets_years(revision: _JdContext, hit: SearchHit) -> bool:
    """年限是否落在硬窗口 ``[n-1, 2n]`` 内；``n`` 缺失时视为满足（不设窗口）。"""
    window = _years_window(revision.min_years)
    if window is None:
        return True
    low, high = window
    years = hit.total_years or 0
    return low <= years <= high


def _career_assessment(revision: _JdContext, candidate_data: dict) -> tuple[float | None, str, str]:
    """职业方向评分与分层：返回 (score, reason, tier)。

    - 两侧都有细分 → 按**细分**比较（合同要求「匹配只按细分」）；
    - 任一侧缺细分 → 回退按**大类**比较，避免存量未回填者失去这一分量；
    - 一侧完全没有方向信息 → None（不参与打分）/ needs_review；
    - JD 方向被候选人全部覆盖 → 1.0 / recommend；部分覆盖 → 覆盖率 / needs_review。

    完全无交集的组合已由 ``evaluate_pair`` 硬淘汰，此处不再重复判定。
    """
    jd_career, jd_specs, _ = confirmed_multi_directions(revision.parsed_data or {})
    cand_career, cand_specs, _ = confirmed_multi_directions(candidate_data)

    if jd_specs and cand_specs:
        basis, candidate_values, label = set(jd_specs), set(cand_specs), "职业细分"
    elif jd_career and cand_career:
        basis, candidate_values, label = set(jd_career), set(cand_career), "职业大类"
    else:
        return None, "一侧方向未确认", "needs_review"

    hit = basis & candidate_values
    if not hit:
        return 0.0, f"{label}不一致", "needs_review"
    ratio = len(hit) / len(basis)
    reason = f"{label}命中 {len(hit)}/{len(basis)}"
    return ratio, reason, ("recommend" if ratio >= 1.0 else "needs_review")


def _business_assessment(revision: _JdContext, candidate_data: dict) -> float | None:
    """业务方向匹配：有交集 1.0、无交集 0.0、任一侧缺失 → None（不参与打分）。"""
    _, _, jd_business = confirmed_multi_directions(revision.parsed_data or {})
    _, _, cand_business = confirmed_multi_directions(candidate_data)
    if not jd_business or not cand_business:
        return None
    return 1.0 if set(jd_business) & set(cand_business) else 0.0


def _industry_match(revision: _JdContext, candidate_data: dict) -> float | None:
    """行业匹配：JD industry 与候选人任一行业字段归一化到规范桶后取交集 → 1.0；否则 0.0；缺失 → None（不参与）。"""
    jd_industry = (revision.parsed_data or {}).get("industry")
    if not jd_industry:
        return None
    jd_buckets = normalize_industries([jd_industry])
    if not jd_buckets:
        return None
    candidate_buckets = normalize_industries([
        candidate_data.get("industry"),
        candidate_data.get("current_industry"),
        candidate_data.get("longest_industry"),
    ])
    if not candidate_buckets:
        return None
    return 1.0 if jd_buckets & candidate_buckets else 0.0


def _build_reason(
    revision: _JdContext,
    hit: SearchHit,
    matched: list[str],
    must_skills: list[str],
    total: float,
    rerank_available: int,
) -> str:
    """Compose a deterministic, evidence-based explanation for a match."""
    parts: list[str] = []
    window = _years_window(revision.min_years)
    if window is not None:
        low, high = window
        years = hit.total_years or 0
        verdict = "落在" if low <= years <= high else "不在"
        parts.append(f"相关经验 {years:g} 年 {verdict} {low:g}~{high:g} 年区间（要求 {revision.min_years:g} 年以上）")
    if must_skills:
        parts.append(f"必备技能覆盖 {len(matched)}/{len(must_skills)}")
    if not rerank_available:
        parts.append("相关性模型不可用，业务分排序")
    parts.append(f"综合得分 {total}")
    return "；".join(parts)
