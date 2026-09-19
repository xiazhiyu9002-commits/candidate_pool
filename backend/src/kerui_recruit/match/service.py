from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, Jd, JdRevision, MatchResult, MatchRun, ResumeDocument, ResumeRevision
from kerui_recruit.direction.policy import confirmed_multi_directions
from kerui_recruit.match.candidate_view import build_candidate_view
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.match.recall import merge_recall_lanes
from kerui_recruit.search.contracts import (
    CandidateFilters,
    SearchHit,
    SearchPage,
)
from kerui_recruit.search.degrees import normalize_degree
from kerui_recruit.search.industry import normalize_industries
from kerui_recruit.search.lexicon import tokenize_lexical_text
from kerui_recruit.search.query import has_skill, normalize_skill
from kerui_recruit.search.live import projection_is_current
from kerui_recruit.search.service import HybridSearchService, _blocking


class MatchEligibilityError(ValueError):
    """Current business entities do not permit starting or recording a match."""


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
                           degraded_reasons=(*page.degraded_reasons, "SCORING_UNAVAILABLE"))
        return replace(page, items=tuple(scored), empty_reason=page.empty_reason if scored else (page.empty_reason or "no_match"))

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
        return [hit for hit, _ in scored[:limit]]

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
            # 三种模式使用不同的查询文本：keyword 用结构化关键词，vector 用 AI 画像/向量文本，
            # hybrid 分别用关键词做 FTS、用向量文本做 embedding。
            if mode == "keyword":
                query = candidate[0].content
                vector_query = None
            elif mode == "vector":
                query = candidate[0].vector_text or candidate[0].content
                vector_query = None
            else:
                query = candidate[0].content
                vector_query = candidate[0].vector_text or candidate[0].content
            page = await self.reverse_search.search(
                query, CandidateFilters(**narrowing), limit=70,
                mode=mode, vector_query=vector_query,
                deadline=deadline - min(.25, max(0., deadline - time.monotonic()) * .1))
            # 双通道召回：救援通道取消方向窄化，救回跨方向/方向待核岗位（按 jd_id 去重）。
            if narrowing:
                rescue_page = await self.reverse_search.search(
                    query, CandidateFilters(), limit=30,
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
            return hit, preferred

    def _reverse_records(self, candidate, job_hits, limit):
        source, preferred = candidate
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
                if filters.min_years is not None and (source.total_years is None or source.total_years < filters.min_years):
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
                              rerank_score=recalled.rerank_score)
                score = self._score_context(context, hit, source_data)
                records.append(ReverseMatchRecord(revision.jd_id, revision.id, company, title, hit, score))
            return sorted(
                (record for record in records
                 if record.score.eligibility != "rejected" and record.score.total >= self._MIN_SCORE),
                key=lambda record: (-record.score.total, -record.hit.score, record.jd_id))[:limit]

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
    parsed = revision.parsed_data or {}
    required = " ".join(parsed.get("required_skills", []))
    skill_must = " ".join(
        req.get("value", "")
        for req in parsed.get("requirements", [])
        if req.get("kind") == "MUST" and req.get("label") in ("技能", "skill")
    )
    duties = " ".join(str(d) for d in (parsed.get("core_duties") or []))
    # 技能 + 核心职责一起进召回：语义通道覆盖 JD 职责，词法通道保留关键概念。
    text = " ".join(part for part in (required, skill_must, duties) if part)
    # 无技能/职责信息时兜底回退到岗位画像/原文，避免 query 为空退化为纯硬过滤。
    return text or (parsed.get("summary") or revision.source_text or "")


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


def _hard_filter(
    revision: _JdContext,
    provided: CandidateFilters | None,
) -> CandidateFilters:
    """JD 硬条件覆盖到 provided 之上，保留 provided 的其余字段（多地点/排除等）。"""
    base = provided if provided is not None else CandidateFilters()
    if revision.min_years is not None:
        base = replace(base, min_years=revision.min_years)
    if revision.highest_degree:
        base = replace(base, highest_degree=normalize_degree(revision.highest_degree))
    # 只对可信的城市地点做硬过滤，脏值（国家/编号）不参与。
    if _is_plausible_location(revision.location):
        base = replace(base, location=revision.location)
    return replace(base, **_career_narrowing(revision.parsed_data or {}))


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


def _meets_years(revision: _JdContext, hit: SearchHit) -> bool:
    if revision.min_years is None:
        return True
    return (hit.total_years or 0) >= revision.min_years


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
    if revision.min_years is not None:
        years = hit.total_years or 0
        verdict = "满足" if years >= revision.min_years else "不满足"
        parts.append(f"相关经验 {years:g} 年 {verdict} {revision.min_years:g} 年要求")
    if must_skills:
        parts.append(f"必备技能覆盖 {len(matched)}/{len(must_skills)}")
    if not rerank_available:
        parts.append("相关性模型不可用，业务分排序")
    parts.append(f"综合得分 {total}")
    return "；".join(parts)
