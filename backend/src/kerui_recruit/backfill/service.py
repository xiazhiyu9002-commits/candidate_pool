"""存量画像回填：为已有候选人/JD 补齐 AI 画像，跳过人工修改与已最新项。

画像生成失败不阻断整个批次；逐条失败被记录，成功项写入数据库并重新入队
索引同步。以当前版本作为幂等键，输入未变化且画像非 stale 时跳过。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.jd.profile_constraints import normalize_constraints
from kerui_recruit.providers.ai.contracts import ExecutionContext
from kerui_recruit.search.sync import enqueue_sync


def _has_dual_form(data: dict, entity_type: str) -> bool:
    """旧记录可能只有整体段落而缺分点/叙述：判定双形态字段是否已补齐。"""
    if entity_type == "candidate":
        return bool(data.get("ai_profile_narrative") and data.get("ai_profile_points"))
    return bool(data.get("candidate_profile_narrative") and data.get("candidate_profile_points"))


# 路由层熔断（无任何可用供应商）：后续每一条都会以同样原因失败，继续逐条空转没有意义。
_FATAL_BACKFILL_CODES = frozenset({"E_AI_NO_PROVIDER"})

# 逐条失败若不记录错误码，批次最终只剩一句「失败 N 条」，无法区分限流、熔断还是解析退化。
_ERROR_CODE_FALLBACK = "E_BACKFILL_ITEM_FAILED"


def _error_entry(entity_id: str | None, entity_type: str, error: Exception) -> dict:
    """把单条失败转成可上报记录，保留错误码与细节，便于事后定位根因。"""
    entry: dict = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "error": type(error).__name__,
    }
    code = getattr(error, "code", None)
    entry["code"] = code if isinstance(code, str) and code else _ERROR_CODE_FALLBACK
    details = getattr(error, "details", None)
    if isinstance(details, (list, tuple)) and details:
        entry["details"] = [str(item) for item in details]
    return entry


class BackfillNoProgressError(RuntimeError):
    """整批零更新且存在失败：必须让任务失败，不能以 SUCCESS 结束从而掩盖失败。"""

    def __init__(self, code: str, user_message: str, *, details: tuple[str, ...] = ()) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.details = details


def no_progress_error(result: dict, *, kind: str) -> BackfillNoProgressError | None:
    """批次零更新时给出可上报的错误；有更新、本就无失败或没有条目时返回 None。"""
    if not result.get("total") or result.get("updated") or not result.get("failed"):
        return None
    codes = {entry.get("code") for entry in result.get("errors", []) if entry.get("code")}
    code = next(iter(codes)) if len(codes) == 1 else "E_BACKFILL_NO_PROGRESS"
    return BackfillNoProgressError(
        code,
        f"{kind}画像回填未更新任何记录（失败 {result['failed']}/{result['total']}）",
        details=tuple(sorted(codes)) or (code,),
    )


class BackfillService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        candidate_generator=None,
        jd_generator=None,
    ) -> None:
        self.session_factory = session_factory
        self.candidate_generator = candidate_generator
        self.jd_generator = jd_generator

    async def backfill_candidate_profiles(
        self, *, report=None, force: bool = False, limit: int | None = None
    ) -> dict:
        if self.candidate_generator is None:
            return {"total": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
        with self.session_factory() as session:
            revisions = list(session.scalars(
                select(ResumeRevision)
                .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
                .where(
                    ResumeRevision.is_current.is_(True),
                    ResumeRevision.status == "READY",
                    Candidate.deleted_at.is_(None),
                    Candidate.status.not_in(("ARCHIVED", "PENDING_REVIEW")),
                )
            ).all())
        return await self._backfill_profiles(
            revisions if limit is None else revisions[:limit],
            generator=self.candidate_generator,
            profile_field="ai_profile_summary",
            source_field="ai_profile_source",
            hash_field="ai_profile_input_hash",
            stale_field="ai_profile_stale",
            entity_type="candidate",
            report=report,
            force=force,
        )

    async def backfill_jd_profiles(
        self, *, report=None, force: bool = False, limit: int | None = None
    ) -> dict:
        if self.jd_generator is None:
            return {"total": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
        with self.session_factory() as session:
            revisions = list(session.scalars(
                select(JdRevision)
                .join(Jd, Jd.id == JdRevision.jd_id)
                .where(
                    JdRevision.is_current.is_(True),
                    JdRevision.status == "READY",
                    Jd.status == "OPEN",
                    Jd.deleted_at.is_(None),
                )
            ).all())
        return await self._backfill_profiles(
            revisions if limit is None else revisions[:limit],
            generator=self.jd_generator,
            profile_field="candidate_profile",
            source_field="candidate_profile_source",
            hash_field="candidate_profile_input_hash",
            stale_field="candidate_profile_stale",
            entity_type="jd",
            report=report,
            force=force,
        )

    async def regenerate_candidate_profile(self, candidate_id: str, instruction: str | None = None) -> dict:
        """为单个候选人重新生成 AI 画像（仅生成预览，不落库、不重建索引）。

        走与批量回填同一条双形态链路（整体段落 + 分点 + 浓缩同源），避免「重新生成」
        产出单文本、保存时再按标点伪拆点。保存由前端「保存」按钮调用字段更新接口完成。
        """
        if self.candidate_generator is None:
            raise RuntimeError("画像生成服务未配置")
        with self.session_factory() as session:
            revision = session.scalar(
                select(ResumeRevision)
                .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                .where(
                    ResumeDocument.candidate_id == candidate_id,
                    ResumeRevision.is_current.is_(True),
                    ResumeRevision.status == "READY",
                )
                .order_by(ResumeRevision.created_at.desc())
                .limit(1)
            )
            if revision is None or revision.parsed_data is None:
                raise LookupError("候选人简历不存在")
            data = dict(revision.parsed_data)
        previous = data.get("ai_profile_summary")
        pair = await self.candidate_generator.generate_pair(data, instruction=instruction, previous=previous)
        new_hash = self.candidate_generator.input_hash(data)
        return {
            "generated": True,
            "summary": pair.narrative,
            "points": [{"text": p.text.strip(), "evidence_paths": list(p.evidence_paths)} for p in pair.points if p.text.strip()],
            "compact": pair.compact,
            "input_hash": new_hash,
        }

    async def regenerate_jd_profile(self, jd_id: str, instruction: str | None = None) -> dict:
        """为单个 JD 重新生成候选人画像要求（仅生成预览，不落库、不重建索引）。

        走与批量回填同一条双形态链路。保存由前端「保存」按钮调用字段更新接口完成。
        """
        if self.jd_generator is None:
            raise RuntimeError("画像生成服务未配置")
        with self.session_factory() as session:
            revision = session.scalar(
                select(JdRevision)
                .where(JdRevision.jd_id == jd_id, JdRevision.is_current.is_(True), JdRevision.status == "READY")
                .order_by(JdRevision.created_at.desc())
                .limit(1)
            )
            if revision is None or revision.parsed_data is None:
                raise LookupError("JD 版本不存在")
            data = dict(revision.parsed_data)
        previous = data.get("candidate_profile")
        pair = await self.jd_generator.generate_pair(data, instruction=instruction, previous=previous)
        new_hash = self.jd_generator.input_hash(data)
        result = {
            "generated": True,
            "summary": pair.narrative,
            "points": [{"text": p.text.strip(), "evidence_paths": list(p.evidence_paths)} for p in pair.points if p.text.strip()],
            "compact": pair.compact,
            "input_hash": new_hash,
        }
        # 硬条件与画像同一次调用产出；退化路径拿不到时不返回该字段，让前端保留既有约束。
        constraints = normalize_constraints(getattr(pair, "exact_constraints", None))
        if constraints:
            result["constraints"] = constraints
        return result

    async def _backfill_profiles(
        self,
        revisions,
        *,
        generator,
        profile_field: str,
        source_field: str | None,
        hash_field: str | None,
        stale_field: str | None,
        entity_type: str,
        report=None,
        force: bool = False,
    ) -> dict:
        total = 0
        updated = 0
        skipped = 0
        failed = 0
        errors: list[dict] = []
        total_count = len(revisions)

        def progress() -> None:
            if report is None:
                return
            report(100 if total_count == 0 else int(total * 100 / total_count))

        for revision in revisions:
            total += 1
            data = dict(revision.parsed_data or {})
            overrides = revision.manual_overrides or {}
            if profile_field in overrides or (source_field and data.get(source_field) == "manual"):
                skipped += 1
                progress()
                continue
            if not force and hash_field is not None:
                new_hash = generator.input_hash(data)
                if (data.get(profile_field) and not data.get(stale_field)
                        and data.get(hash_field) == new_hash
                        and _has_dual_form(data, entity_type)):
                    skipped += 1
                    progress()
                    continue
            else:
                new_hash = None
            try:
                if hasattr(generator, "generate_pair"):
                    pair = await generator.generate_pair(data, execution_context=ExecutionContext.BATCH)
                    summary = pair.narrative
                    consistent = pair.is_consistent()
                    points = [p.text.strip() for p in pair.points if p.text.strip()]
                    point_dicts = [
                        {"text": p.text.strip(), "evidence_paths": list(p.evidence_paths)}
                        for p in pair.points if p.text.strip()
                    ]
                    compact = pair.compact or (points[0][:60] if points else "")
                else:
                    summary = await generator.generate(data, execution_context=ExecutionContext.BATCH)
                    consistent = True
                    points = [line.strip() for line in summary.splitlines() if line.strip()]
                    point_dicts = [{"text": p, "evidence_paths": []} for p in points]
                    compact = points[0][:60] if points else ""
                if not summary:
                    raise ValueError("empty profile")
            except Exception as error:  # noqa: BLE001 - per-item failures must not abort the batch
                failed += 1
                errors.append(_error_entry(revision.id, entity_type, error))
                progress()
                if getattr(error, "code", None) in _FATAL_BACKFILL_CODES:
                    # 熔断：后续每一条都会以同样原因失败，整批立即结束，不再空转上千次。
                    remaining = total_count - total
                    if remaining > 0:
                        failed += remaining
                        errors.append({
                            "entity_id": None,
                            "entity_type": entity_type,
                            "error": "BatchAborted",
                            "code": getattr(error, "code"),
                            "details": [f"熔断后剩余 {remaining} 条未处理"],
                        })
                        total = total_count
                        progress()
                    break
                continue

            with self.session_factory() as session, session.begin():
                current = session.get(type(revision), revision.id)
                if current is None or current.parsed_data is None:
                    skipped += 1
                else:
                    latest = dict(current.parsed_data)
                    latest_overrides = current.manual_overrides or {}
                    if profile_field in latest_overrides or (source_field and latest.get(source_field) == "manual"):
                        skipped += 1
                    elif not consistent:
                        # 关键事实未同时出现在整体与分点：标记待核并保留旧画像，不覆盖。
                        latest[stale_field] = True if stale_field else latest.get(stale_field, False)
                        current.parsed_data = latest
                        skipped += 1
                    else:
                        latest[profile_field] = summary
                        # 双形态画像：整体段落 + 分点 + 浓缩上下文，共享同一次生成结果。
                        if entity_type == "candidate":
                            latest["ai_profile_narrative"] = summary.strip()
                            latest["ai_profile_points"] = point_dicts
                            latest["ai_profile_compact"] = compact
                            latest["ai_profile_version"] = 3
                        else:
                            latest["candidate_profile_narrative"] = summary.strip()
                            latest["candidate_profile_points"] = point_dicts
                            latest["candidate_profile_compact"] = compact
                            latest["candidate_profile_version"] = 3
                        if source_field:
                            latest[source_field] = "ai"
                        if hash_field and new_hash:
                            latest[hash_field] = new_hash
                        if stale_field:
                            latest[stale_field] = False
                        current.parsed_data = latest
                        if entity_type == "candidate":
                            enqueue_sync(session, "candidate", current.document.candidate_id)
                        else:
                            enqueue_sync(session, "jd", current.jd_id)
                        updated += 1
            # 每条提交一次，进度上报放在事务外，避免在持有写事务时再开新连接。
            progress()
        return {"total": total, "updated": updated, "skipped": skipped, "failed": failed, "errors": errors}
