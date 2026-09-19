"""历史方向补齐：只读已有结构化 revision，把存量单值方向升级为多值方向体系。

不调用任何模型，跳过人工修订；用于让存量简历/JD 也能获得
``career_directions`` / ``career_specializations`` / ``business_directions``，
否则新的方向硬门槛在存量数据上会静默失效。
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.direction.classifier import classify_direction
from kerui_recruit.direction.policy import (
    MAX_CAREER_DIRECTIONS,
    MAX_CAREER_DIRECTIONS_JD,
    SPECIALIZATION_PARENT,
    VALID_DIRECTIONS,
    apply_direction_normalization,
    is_pending_career,
    normalize_business_directions,
    normalize_career,
)
from kerui_recruit.search.sync import enqueue_sync

# 人工修订字段名：出现任一即视为人工确认，不再自动复判。
_DIRECTION_OVERRIDE_KEYS = ("direction", "career_directions", "career_specializations", "business_directions")


def _manually_overridden(overrides: object) -> bool:
    return isinstance(overrides, dict) and any(key in overrides for key in _DIRECTION_OVERRIDE_KEYS)


def _report_progress(report: Callable[[int], None] | None, total_count: int, processed: int) -> None:
    if report is None:
        return
    report(100 if total_count == 0 else int(processed * 100 / total_count))


def backfill_directions(
    session_factory: sessionmaker[Session],
    *,
    dry_run: bool = True,
    report: Callable[[int], None] | None = None,
    entity_type: str = "candidate",
) -> dict:
    """对 READY 当前 revision 做方向复判（`entity_type` 为 candidate 或 jd）。

    - 跳过 `manual_overrides` 含方向类字段的记录（人工修订优先，绝不覆盖）。
    - 跳过已确定（`career_directions` 含具体方向）的记录。
    - 否则用 `classify_direction` 基于已有结构化数据复判，写回最新 revision 的
      `direction` / `career_directions` / `career_specializations` /
      `business_directions` / `career_taxonomy_version`，并入队索引同步
      （`dry_run=True` 只统计不写回）。
    """
    if entity_type not in ("candidate", "jd"):
        raise ValueError(f"unsupported entity_type: {entity_type}")

    scanned = 0
    changed = 0
    pending = 0

    with session_factory() as session:
        if entity_type == "candidate":
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
                .order_by(ResumeRevision.id)
            ).all())
        else:
            revisions = list(session.scalars(
                select(JdRevision)
                .join(Jd, Jd.id == JdRevision.jd_id)
                .where(
                    JdRevision.is_current.is_(True),
                    JdRevision.status == "READY",
                    Jd.status == "OPEN",
                    Jd.deleted_at.is_(None),
                )
                .order_by(JdRevision.id)
            ).all())

        total_count = len(revisions)
        for revision in revisions:
            scanned += 1
            data = dict(revision.parsed_data or {})
            overrides = revision.manual_overrides or {}
            if _manually_overridden(overrides):
                _report_progress(report, total_count, scanned)
                continue  # 人工修订跳过
            if not _needs_backfill(data):
                _report_progress(report, total_count, scanned)
                continue  # 已是当前词表产物，跳过
            pending += 1
            if dry_run:
                _report_progress(report, total_count, scanned)
                continue
            decision = classify_direction(data)
            career, specializations = _career_and_specializations(
                data, decision, max_directions=_direction_limit(entity_type))
            business = normalize_business_directions(decision.business_directions)
            data["career_directions"] = list(career)
            data["career_specializations"] = list(specializations)
            data["business_directions"] = list(business)
            # 统一盖章词表版本 + 镜像单值 direction + 同步 direction_assessment。
            apply_direction_normalization(data, max_directions=_direction_limit(entity_type))
            revision.parsed_data = data
            if entity_type == "candidate":
                enqueue_sync(session, "candidate", revision.document.candidate_id)
            else:
                enqueue_sync(session, "jd", revision.jd_id)
            changed += 1
            # 每条提交一次，进度上报放在事务外，避免在持有写事务时再次开新连接。
            session.commit()
            _report_progress(report, total_count, scanned)

    return {"scanned": scanned, "changed": changed, "pending": pending, "errors": 0}


def _needs_backfill(data: dict) -> bool:
    """是否需要补齐多值方向字段。

    判据是「还没有 `career_directions` 多值产物」，而不是「方向为空」：
    存量数据都带旧的**单值** `direction`，若只按「为空」判断，1721 份简历里只有 116 份、
    40 个 JD 里 0 个会被补齐，新的方向字段与硬门槛会整体失效。

    已经有多值产物的记录不再改动（避免把新提示词/人工的高质量结果降级为关键词结果）；
    细分为空属于「无证据」的正常结果，同样不重判。
    """
    return is_pending_career(data.get("career_directions"))


def _direction_limit(entity_type: str) -> int:
    return MAX_CAREER_DIRECTIONS if entity_type == "candidate" else MAX_CAREER_DIRECTIONS_JD


def _career_and_specializations(
    data: dict, decision, *, max_directions: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """按优先级合成职业大类与细分。

    优先级：**已有方向（锚点）> 分类器给出的大类顺序**。

    约束（来自真实数据上发现的缺陷）：
    1. 已有 `direction` 来自模型解析，质量高于关键词复判，必须留在首位——同时保证单值
       `direction` 镜像不被改写；
    2. 细分所属大类必须入选，否则该细分会被整体丢弃。这一条由 `classify_direction`
       在源头保证（它已把有细分支撑的大类并入大类列表），这里不再重复处理。

    传入 `normalize_career` 的大类数已压到上限以内，避免它重排而把锚点挤走。
    """
    anchor = next((value for value in _current_directions(data) if value != "OTHER"), None)
    specializations = tuple(
        code for code in decision.specializations if code in SPECIALIZATION_PARENT)

    ordered: list[str] = []
    for value in ((anchor,) if anchor else ()) + tuple(decision.career_directions):
        if value in VALID_DIRECTIONS and value != "OTHER" and value not in ordered:
            ordered.append(value)
    if not ordered:
        return (), ()

    return normalize_career(
        ordered[:max_directions], specializations, max_directions=max_directions)


def _current_directions(data: dict) -> tuple[str, ...]:
    """现有可用方向：优先顶层 career_directions，回退单值 direction（存量兼容）。"""
    raw = data.get("career_directions")
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(v) for v in raw)
    if isinstance(raw, str) and raw:
        return (raw,)
    single = data.get("direction")
    return (str(single),) if single else ()


def audit_all_non_manual(
    session_factory: sessionmaker[Session],
    *,
    entity_type: str = "candidate",
) -> dict:
    """审计所有非人工覆盖记录的方向（含已确认但可能错误的），dry run 报告。

    返回拟改动数、冲突数（合法枚举但复判方向不同）、复判置信度分布、人工覆盖数。
    不写回、不入队同步；实际写回由调用方分批执行（先空/OTHER，再高置信误分）。
    """
    if entity_type not in ("candidate", "jd"):
        raise ValueError(f"unsupported entity_type: {entity_type}")

    scanned = 0
    manual = 0
    would_change = 0
    conflict = 0
    confidence_counts = {"high": 0, "medium": 0, "low": 0}

    with session_factory() as session:
        if entity_type == "candidate":
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
                .order_by(ResumeRevision.id)
            ).all())
        else:
            revisions = list(session.scalars(
                select(JdRevision)
                .join(Jd, Jd.id == JdRevision.jd_id)
                .where(
                    JdRevision.is_current.is_(True),
                    JdRevision.status == "READY",
                    Jd.status == "OPEN",
                    Jd.deleted_at.is_(None),
                )
                .order_by(JdRevision.id)
            ).all())

        for revision in revisions:
            scanned += 1
            overrides = revision.manual_overrides or {}
            if _manually_overridden(overrides):
                manual += 1
                continue
            data = dict(revision.parsed_data or {})
            current = _current_directions(data)
            decision = classify_direction(data)
            confidence_counts[decision.confidence] = confidence_counts.get(decision.confidence, 0) + 1
            if set(decision.career_directions) != set(current):
                would_change += 1
                if not is_pending_career(current):
                    conflict += 1  # 已有具体方向但复判不同（可能错误）

    return {
        "scanned": scanned,
        "manual": manual,
        "would_change": would_change,
        "conflict": conflict,
        "confidence": confidence_counts,
    }
