"""Durable index work is enqueued in the same transaction as business changes."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, select, update

from kerui_recruit.db.models import Candidate, IndexSyncRecord, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.direction.policy import extract_multi_directions
from kerui_recruit.search.cities import normalize_location_terms
from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lexicon import tokenize_lexical_text

# 投影并发：一个实体一条任务、每条一次批量 embedding（实测每人中位 3.0k 字符 / 18 个 chunk）。
# 串行时全量重嵌会被网络往返拖成小时级，并发只解决「同时在飞几个」。
DEFAULT_SYNC_CONCURRENCY = 4

# embedding 上游配额：TPM 500,000（RPM 2,000 远不是约束——TPM 打满时也只有约 3 请求/秒）。
# 取 80% 留余量给交互式检索查询，避免后台重嵌把配额吃光导致搜索报限流。
EMBEDDING_TOKENS_PER_MINUTE = 400_000


class _TokenRateLimiter:
    """按 token/分钟放行：并发决定同时在飞几个请求，吞吐由这里的预算兜住。

    没有 tokenizer，用字符数当 token 数的保守代理（中文里 1 字符 ≈ 1 token 或略少）：
    宁可少发一点，也不要把上游 TPM 打爆。
    """

    def __init__(self, tokens_per_minute: float) -> None:
        self._capacity = max(1.0, float(tokens_per_minute))
        self._available = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float) -> None:
        cost = max(1.0, float(tokens))
        while True:
            async with self._lock:
                now = time.monotonic()
                self._available = min(
                    self._capacity, self._available + (now - self._updated) * self._capacity / 60.0)
                self._updated = now
                if self._available >= cost:
                    self._available -= cost
                    return
                wait = (cost - self._available) * 60.0 / self._capacity
            await asyncio.sleep(min(max(wait, 0.05), 1.0))


def enqueue_sync(session: Session, entity_type: str, entity_id: str, mode: str = "FULL") -> None:
    if entity_type not in ("candidate", "jd"):
        raise ValueError("Unsupported index entity")
    if mode not in ("FULL", "METADATA"):
        raise ValueError("Unsupported sync mode")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 计算最终模式：FULL 总是 FULL；METADATA 只有在「不存在待处理 FULL」时才保持 METADATA。
    # 待处理判断必须用 requested_version > applied_version，不能只看 requested_mode。
    existing = session.scalar(select(IndexSyncRecord).where(
        IndexSyncRecord.entity_type == entity_type, IndexSyncRecord.entity_id == entity_id))
    final_mode = mode
    if mode == "METADATA" and existing is not None \
            and existing.requested_version > existing.applied_version \
            and existing.requested_mode == "FULL":
        final_mode = "FULL"
    stmt = insert(IndexSyncRecord).values(entity_type=entity_type, entity_id=entity_id,
        requested_mode=final_mode, requested_version=1, applied_version=0, status="PENDING", attempts=0)
    set_values: dict = {"requested_version": IndexSyncRecord.requested_version + 1,
                        "requested_mode": final_mode, "status": "PENDING",
                        "next_attempt_at": None, "last_error": None, "updated_at": now}
    session.execute(stmt.on_conflict_do_update(index_elements=["entity_type", "entity_id"],
        set_=set_values))


def build_jd_documents(jd, revision) -> list[dict]:
    """把 JD 拆成父/子 chunk 文档，供索引分片检索。

    父 chunk 承载整体语义（岗位名 + 公司 + 画像 + 技能），子 chunk 承载每个
    职责点与每个必需技能，避免整份 JD 原文一个向量导致的语义漂移。
    """
    parsed = revision.parsed_data or {}
    company = jd.company or ""
    title = jd.title or ""
    career_directions, career_specializations, business_directions = extract_multi_directions(parsed)
    skills = [str(s) for s in (parsed.get("required_skills") or []) if str(s).strip()]
    plus_skills = [str(s) for s in (parsed.get("plus_skills") or []) if str(s).strip()]
    duties = [str(d) for d in (parsed.get("core_duties") or []) if str(d).strip()]

    parent_parts = [title, company, parsed.get("summary") or "",
                    parsed.get("candidate_profile_narrative") or parsed.get("candidate_profile") or "", *skills]
    parent_text = " ".join(p for p in parent_parts if p and str(p).strip())
    # 向量语义额外纳入加分技能（有则更优）；FTS 关键词只保留核心必备技能。
    vector_text = " ".join(p for p in (*parent_parts, *plus_skills) if p and str(p).strip()) or parent_text

    hard = {
        "revision_id": revision.id,
        "company": company,
        "title": title,
        "min_years": float(revision.min_years) if revision.min_years is not None else None,
        "highest_degree": revision.highest_degree,
        "location": revision.location,
        "direction": parsed.get("direction"),
        "career_directions": list(career_directions),
        "career_specializations": list(career_specializations),
        "business_directions": list(business_directions),
    }

    documents: list[dict] = []
    if parent_text.strip():
        documents.append({
            **hard,
            "keyword_text": parent_text,
            "keyword_index_text": " ".join(tokenize_lexical_text(parent_text)),
            "vector_text": vector_text,
            "chunk_type": "parent",
            "parent_id": None,
        })

    for duty in duties:
        documents.append({
            **hard,
            "keyword_text": duty,
            "keyword_index_text": " ".join(tokenize_lexical_text(duty)),
            "vector_text": duty,
            "chunk_type": "child",
            "parent_id": revision.id,
        })
    for skill in skills:
        documents.append({
            **hard,
            "keyword_text": skill,
            "keyword_index_text": " ".join(tokenize_lexical_text(skill)),
            "vector_text": skill,
            "chunk_type": "child",
            "parent_id": revision.id,
        })

    # 候选人画像分点作为独立子切片（前缀为画像浓缩上下文，不淹没具体职责/技能）。
    profile_points: list[str] = []
    for p in (parsed.get("candidate_profile_points") or []):
        if isinstance(p, dict):
            text = str(p.get("text") or "").strip()
        else:
            text = str(p).strip()
        if text:
            profile_points.append(text)
    if not profile_points and (parsed.get("candidate_profile") or "").strip():
        profile_points = [line.strip() for line in str(parsed["candidate_profile"]).splitlines() if line.strip()]
    compact = str(parsed.get("candidate_profile_compact") or "").strip() or (profile_points[0][:60] if profile_points else "")
    prefix = f"{compact} " if compact else ""
    for point in profile_points:
        documents.append({
            **hard,
            "keyword_text": point,
            "keyword_index_text": " ".join(tokenize_lexical_text(point)),
            "vector_text": f"{prefix}{point}".strip(),
            "chunk_type": "child",
            "parent_id": revision.id,
        })

    # 兜底：既无画像也无技能时，退回整份原文，避免索引为空。
    if not documents:
        fallback = " ".join((company, title, revision.source_text or ""))
        documents.append({
            **hard,
            "keyword_text": fallback,
            "keyword_index_text": " ".join(tokenize_lexical_text(fallback)),
            "vector_text": fallback,
            "chunk_type": "parent",
            "parent_id": None,
        })
    return documents


def build_jd_documents_b(jd, revision) -> list[dict]:
    """JD 变体 B：只保留整体画像父段与职责子段，去掉单技能短子向量。

    必备技能仍保留在父 chunk 的 keyword_text/keyword_index_text（词法面）与硬规则
    中供 FTS 精确命中，不再为每个技能单独生成短向量，降低子向量数量与成本。
    """
    parsed = revision.parsed_data or {}
    company = jd.company or ""
    title = jd.title or ""
    career_directions, career_specializations, business_directions = extract_multi_directions(parsed)
    skills = [str(s) for s in (parsed.get("required_skills") or []) if str(s).strip()]
    plus_skills = [str(s) for s in (parsed.get("plus_skills") or []) if str(s).strip()]
    duties = [str(d) for d in (parsed.get("core_duties") or []) if str(d).strip()]

    parent_parts = [title, company, parsed.get("summary") or "",
                    parsed.get("candidate_profile_narrative") or parsed.get("candidate_profile") or "", *skills]
    parent_text = " ".join(p for p in parent_parts if p and str(p).strip())
    vector_text = " ".join(p for p in (*parent_parts, *plus_skills) if p and str(p).strip()) or parent_text

    hard = {
        "revision_id": revision.id,
        "company": company,
        "title": title,
        "min_years": float(revision.min_years) if revision.min_years is not None else None,
        "highest_degree": revision.highest_degree,
        "location": revision.location,
        "direction": parsed.get("direction"),
        "career_directions": list(career_directions),
        "career_specializations": list(career_specializations),
        "business_directions": list(business_directions),
    }

    documents: list[dict] = []
    if parent_text.strip():
        documents.append({
            **hard,
            "keyword_text": parent_text,
            "keyword_index_text": " ".join(tokenize_lexical_text(parent_text)),
            "vector_text": vector_text,
            "chunk_type": "parent",
            "parent_id": None,
        })
    for duty in duties:
        documents.append({
            **hard,
            "keyword_text": duty,
            "keyword_index_text": " ".join(tokenize_lexical_text(duty)),
            "vector_text": duty,
            "chunk_type": "child",
            "parent_id": revision.id,
        })

    if not documents:
        fallback = " ".join((company, title, revision.source_text or ""))
        documents.append({
            **hard,
            "keyword_text": fallback,
            "keyword_index_text": " ".join(tokenize_lexical_text(fallback)),
            "vector_text": fallback,
            "chunk_type": "parent",
            "parent_id": None,
        })
    return documents


class IndexSyncService:
    """Coalesced, retryable projection with optimistic generation checks.

    Embedding runs outside the SQLite write lock. Publication checks the outbox
    generation under BEGIN IMMEDIATE so an older asynchronous completion cannot
    acknowledge or overwrite a newer committed business change.

    一次投影 = 一个实体一条 outbox 任务 = 一次批量 embedding 调用（该实体的全部 chunk 文本）。
    全量重嵌时串行会把时间线性拉长，所以这里按 ``DEFAULT_SYNC_CONCURRENCY`` 并发；
    并发只决定「同时在飞几个请求」，吞吐由 ``_TokenRateLimiter`` 的 token/分钟预算兜住，
    避免把上游 TPM 配额打爆（RPM 不是约束：TPM 打满时也只有约 3 请求/秒）。
    发布步骤本身仍由 ``BEGIN IMMEDIATE`` 串行化（写 Lance 也在该事务内），所以并发只影响
    快照与 embedding 这两步，索引写入仍是单写者。
    """

    def __init__(self, *, session_factory, index, embedding_provider, jd_index=None,
                 rebuild_tracker=None, tokens_per_minute: float = EMBEDDING_TOKENS_PER_MINUTE):
        self.session_factory = session_factory
        self.index = index
        self.embedding_provider = embedding_provider
        self.jd_index = jd_index
        self.rebuild_tracker = rebuild_tracker
        self._run_lock = asyncio.Lock()
        # 发布锁：`_publish` 用 BEGIN IMMEDIATE 独占 SQLite 写锁，而事务里还夹着一次
        # LanceDB 提交，锁持有时间远大于 busy_timeout(5s)。并发 worker 同时进入必然互相
        # 超时，表现为 outbox 长期 RETRY_WAIT + last_error=OperationalError（存量 1,500+
        # 实体一直投不进去就是这个原因）。因此**发布串行、快照与 embedding 保持并发**。
        self._publish_lock = asyncio.Lock()
        self._embedding_budget = _TokenRateLimiter(tokens_per_minute)

    async def run_once(self, *, batch_size: int = 25, force: bool = False,
                       entity_type: str | None = None, entity_id: str | None = None,
                       concurrency: int = DEFAULT_SYNC_CONCURRENCY) -> int:
        async with self._run_lock:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with self.session_factory() as session:
                stmt = select(IndexSyncRecord).where(IndexSyncRecord.requested_version > IndexSyncRecord.applied_version)
                if entity_type is not None:
                    stmt = stmt.where(IndexSyncRecord.entity_type == entity_type)
                if entity_id is not None:
                    stmt = stmt.where(IndexSyncRecord.entity_id == entity_id)
                if not force:
                    stmt = stmt.where(or_(IndexSyncRecord.next_attempt_at.is_(None), IndexSyncRecord.next_attempt_at <= now))
                jobs = [(r.id, r.entity_type, r.entity_id, r.requested_version, r.requested_mode) for r in session.scalars(
                    stmt.order_by(IndexSyncRecord.updated_at, IndexSyncRecord.id).limit(batch_size))]
            completed = 0
            pending = iter(jobs)

            async def worker() -> None:
                nonlocal completed
                while True:
                    try:
                        job_id, kind, target_id, generation, _mode = next(pending)
                    except StopIteration:
                        return
                    try:
                        applied = await self._project_one(job_id, kind, target_id, generation)
                        completed += int(applied)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await asyncio.to_thread(self._failed, job_id, generation, error)

            await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
            return completed

    async def _project_one(self, job_id: str, kind: str, entity_id: str, generation: int) -> bool:
        """投影一个实体：快照 → 批量 embedding → 发布。返回是否真的提交。"""
        snapshot = await asyncio.to_thread(self._snapshot, kind, entity_id)
        target = self.index if kind == "candidate" else self.jd_index
        if target is None:
            raise RuntimeError("Index consumer is not configured")
        underlying = getattr(target, "index", target)
        if snapshot is not None:
            if hasattr(underlying, "is_compatible") and not underlying.is_compatible():
                raise RuntimeError("Index version requires rebuild")
            documents = snapshot["documents"]
            texts = [doc["vector_text"] for doc in documents]
            cached = []
            for revision_id in dict.fromkeys(doc["revision_id"] for doc in documents):
                cached.extend(await asyncio.to_thread(underlying.get_revision_chunks, revision_id))
            by_text = {row["vector_text"]: row["vector"] for row in cached}
            missing = [text for text in texts if text not in by_text]
            if missing:
                await self._embedding_budget.acquire(sum(len(text) for text in missing))
                vectors = await asyncio.wait_for(self.embedding_provider.embed_documents(missing), timeout=60)
                if len(vectors) != len(missing):
                    raise ValueError("Embedding response count mismatch")
                by_text.update(zip(missing, vectors))
            snapshot["vectors"] = [by_text[text] for text in texts]
        async with self._publish_lock:
            return await asyncio.to_thread(self._publish, job_id, kind, entity_id, generation, snapshot)

    def _snapshot(self, kind, entity_id):
        with self.session_factory() as session:
            if kind == "candidate":
                candidate = session.get(Candidate, entity_id)
                if candidate is None or candidate.deleted_at is not None or candidate.status in ("ARCHIVED", "PENDING_REVIEW"):
                    return None
                revisions = list(session.scalars(select(ResumeRevision).join(ResumeDocument).where(
                    ResumeDocument.candidate_id == entity_id, ResumeRevision.is_current.is_(True),
                    ResumeRevision.status == "READY").order_by(ResumeRevision.created_at.desc(), ResumeRevision.id.desc())))
                if not revisions:
                    return None
                from kerui_recruit.schools.reference import SchoolReference
                from kerui_recruit.search.documents import build_candidate_document, build_child_documents
                from kerui_recruit.match.candidate_view import build_candidate_view
                school_alias_groups = SchoolReference(self.session_factory).alias_groups()
                # 同一候选人的各 chunk 写统一硬字段（方向/地点取最近修订，年限/学历优先候选人表）。
                view = build_candidate_view(revisions, candidate)
                unified_direction = view.get("direction")
                unified_career = tuple(view.get("career_directions") or ())
                unified_specs = tuple(view.get("career_specializations") or ())
                unified_business = tuple(view.get("business_directions") or ())
                unified_location = view.get("location")
                unified_total_years = (float(candidate.total_years)
                                       if candidate.total_years is not None else view.get("total_years"))
                unified_highest_degree = candidate.highest_degree or view.get("highest_degree")
                documents: list[dict] = []
                preferred: set[str] = set()
                for revision in revisions:
                    data = dict(revision.parsed_data or {})
                    locations = data.get("preferred_locations") or data.get("preferred_location") or []
                    if isinstance(locations, str):
                        from kerui_recruit.search.query import parse_query
                        locations = parse_query("期望" + locations).filters.preferred_locations or (locations,)
                    preferred.update(locations)
                    doc = build_candidate_document(
                        data,
                        display_name=candidate.display_name,
                        school_alias_groups=school_alias_groups,
                    )
                    if not doc["keyword_text"].strip() and not doc["vector_text"].strip():
                        continue
                    doc.update({
                        "revision_id": revision.id,
                        "total_years": unified_total_years,
                        "highest_degree": unified_highest_degree,
                        "location": unified_location,
                        # location_terms 必须与 location 列**同源**：现居硬过滤读的是
                        # location_terms，界面显示的是 location。多修订候选人若按各自
                        # 修订的 location 生成词项，就会出现「显示苏州、按无锡筛」这类
                        # 不一致（实测 6/1477 行）。归一化也在这一步，口径与查询词典同源。
                        "location_terms": list(normalize_location_terms(unified_location)),
                        "direction": unified_direction,
                        "career_directions": list(unified_career),
                        "career_specializations": list(unified_specs),
                        "business_directions": list(unified_business),
                        "candidate_status": candidate.status,
                        "qs_rank": data.get("qs_rank"),
                        "preferred_locations": tuple(sorted(preferred)),
                        "chunk_type": "parent",
                        "parent_id": None,
                    })
                    documents.append(doc)
                    # 子 chunk：继承父的全部硬字段与 term（保证 _where 精确筛选对父/子一致），
                    # 仅把文本与向量替换为单段经历/项目的片段，用于精准语义/词级召回。
                    for child in build_child_documents(data):
                        child_doc = dict(doc)
                        child_doc["keyword_text"] = child["vector_text"]
                        child_doc["keyword_index_text"] = child["keyword_index_text"]
                        child_doc["vector_text"] = child["vector_text"]
                        child_doc["chunk_type"] = "child"
                        child_doc["parent_id"] = revision.id
                        child_doc["kind"] = child["kind"]
                        child_doc["sequence"] = child.get("sequence")
                        child_doc["evidence_path"] = child.get("evidence_path") or []
                        documents.append(child_doc)
                if not documents:
                    return None
                return {"documents": documents}
            jd = session.get(Jd, entity_id)
            if jd is None or jd.deleted_at is not None or jd.status != "OPEN":
                return None
            revision = session.scalar(select(JdRevision).where(JdRevision.jd_id == entity_id,
                JdRevision.is_current.is_(True), JdRevision.status == "READY")
                .order_by(JdRevision.created_at.desc()).limit(1))
            if revision is None:
                return None
            documents = build_jd_documents(jd, revision)
            if not documents:
                return None
            return {"documents": documents}

    def _publish(self, job_id, kind, entity_id, generation, snapshot):
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                job = session.get(IndexSyncRecord, job_id)
                if job is None or job.requested_version != generation or job.applied_version >= generation:
                    session.rollback()
                    return False
                if kind == "candidate":
                    if snapshot is None:
                        self.index.delete_candidate(entity_id)
                    else:
                        chunks = [
                            SearchChunk(
                                id=f"{doc['revision_id']}:{i}",
                                candidate_id=entity_id,
                                revision_id=doc["revision_id"],
                                content=doc["keyword_text"],
                                vector=tuple(vector),
                                total_years=doc.get("total_years"),
                                highest_degree=doc.get("highest_degree"),
                                location=doc.get("location"),
                                candidate_status=doc.get("candidate_status", "AVAILABLE"),
                                qs_rank=doc.get("qs_rank"),
                                preferred_locations=tuple(doc.get("preferred_locations") or ()),
                                keyword_text=doc["keyword_text"],
                                keyword_index_text=doc["keyword_index_text"],
                                vector_text=doc["vector_text"],
                                body_index_text=doc.get("body_index_text") or "",
                                chunk_type=doc.get("chunk_type", "parent"),
                                parent_id=doc.get("parent_id"),
                                kind=doc.get("kind") or doc.get("chunk_type") or "parent",
                                sequence=doc.get("sequence"),
                                evidence_path=tuple(doc.get("evidence_path") or ()),
                                name_terms=tuple(doc.get("name_terms") or ()),
                                school_terms=tuple(doc.get("school_terms") or ()),
                                company_terms=tuple(doc.get("company_terms") or ()),
                                title_terms=tuple(doc.get("title_terms") or ()),
                                location_terms=tuple(doc.get("location_terms") or ()),
                                skills=tuple(doc.get("skills") or ()),
                                age=doc.get("age"),
                                school_tags=tuple(doc.get("school_tags") or ()),
                                direction=doc.get("direction"),
                                school_region=doc.get("school_region"),
                                specializations=tuple(doc.get("specializations") or ()),
                                career_directions=tuple(doc.get("career_directions") or ()),
                                career_specializations=tuple(doc.get("career_specializations") or ()),
                                business_directions=tuple(doc.get("business_directions") or ()),
                            )
                            for i, (doc, vector) in enumerate(zip(snapshot["documents"], snapshot["vectors"]))
                        ]
                        self.index.replace_candidate(chunks)
                elif snapshot is None:
                    self.jd_index.delete_jd(entity_id)
                else:
                    documents = snapshot["documents"]
                    head = documents[0]
                    self.jd_index.upsert_many(jd_id=entity_id, revision_id=head["revision_id"],
                        documents=documents, vectors=snapshot["vectors"],
                        company=head.get("company", ""), title=head.get("title", ""),
                        min_years=head.get("min_years"), highest_degree=head.get("highest_degree"),
                        location=head.get("location"), direction=head.get("direction"),
                        career_directions=tuple(head.get("career_directions") or ()),
                        career_specializations=tuple(head.get("career_specializations") or ()),
                        business_directions=tuple(head.get("business_directions") or ()))
                job.applied_version = generation
                job.status, job.last_error, job.next_attempt_at = "SYNCED", None, None
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise

    def _failed(self, job_id, generation, error):
        with self.session_factory() as session, session.begin():
            job = session.get(IndexSyncRecord, job_id)
            if job is None or job.requested_version != generation:
                return
            job.attempts += 1
            job.status = "RETRY_WAIT"
            # No raw provider payload, resume text or credentials in diagnostics.
            job.last_error = type(error).__name__
            job.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))

    def status(self):
        with self.session_factory() as session:
            pending = IndexSyncRecord.requested_version > IndexSyncRecord.applied_version
            total = session.scalar(select(func.count()).select_from(IndexSyncRecord).where(pending))
            failed = session.scalar(select(func.count()).select_from(IndexSyncRecord).where(pending, IndexSyncRecord.status == "RETRY_WAIT"))
            jobs = list(session.scalars(select(IndexSyncRecord).where(pending)
                .order_by(IndexSyncRecord.updated_at, IndexSyncRecord.id).limit(100)))
            indexes = []
            for kind, target in (("candidate", self.index), ("jd", self.jd_index)):
                underlying = getattr(target, "index", target)
                indexes.append({"entity_type": kind, "compatible": bool(underlying and underlying.is_compatible()),
                    "error": underlying.compatibility_error if underlying else "Index is not configured"})
            return {"pending": total, "failed": failed, "indexes": indexes,
                    "rebuild": self.rebuild_tracker.snapshot() if self.rebuild_tracker else None,
                    "items": [{"entity_type": j.entity_type, "entity_id": j.entity_id,
                               "status": j.status, "attempts": j.attempts, "error": j.last_error} for j in jobs]}

    def retry_pending(self):
        with self.session_factory() as session, session.begin():
            session.execute(update(IndexSyncRecord).where(IndexSyncRecord.requested_version > IndexSyncRecord.applied_version)
                .values(status="PENDING", next_attempt_at=None, last_error=None))
        return self.status()

    async def run_forever(self, *, interval_seconds: float = 1.0):
        while True:
            try:
                await self.run_once()
                if self.rebuild_tracker is not None and self.rebuild_tracker.active:
                    # 排空即视为重建完成：落完成时间并清理归档的旧索引。
                    self.rebuild_tracker.poll(pending=self.status()["pending"])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("Index sync cycle failed: %s", type(error).__name__)
            await asyncio.sleep(interval_seconds)
