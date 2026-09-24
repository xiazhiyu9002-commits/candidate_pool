from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kerui_recruit.db.base import Base, IdMixin, utc_now


class Candidate(IdMixin, Base):
    __tablename__ = "candidate"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_REVIEW','AVAILABLE','ON_HOLD','ARCHIVED')",
            name="ck_candidate_status",
        ),
        Index("ix_candidate_active", "deleted_at", "status"),
    )

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE", nullable=False)
    workflow_previous_status: Mapped[str | None] = mapped_column(String(32))
    total_years: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    highest_degree: Mapped[str | None] = mapped_column(String(32))
    # 沟通记录：单条自由文本，记录「性格不好、不乐意沟通」这类偏软性信息。
    # 刻意**不**进画像输入、不进索引文档、不参与向量：这类主观判断一旦混进画像，
    # 会顺着画像污染向量与检索结果；它只需要一个地方存下来并展示。
    communication_note: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    documents: Mapped[list[ResumeDocument]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )
    contact: Mapped[CandidateContact | None] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class CandidateContact(IdMixin, Base):
    __tablename__ = "candidate_contact"

    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    email_encrypted: Mapped[str | None] = mapped_column(Text)
    phone_encrypted: Mapped[str | None] = mapped_column(Text)
    email_confidence: Mapped[float | None] = mapped_column(Float)
    phone_confidence: Mapped[float | None] = mapped_column(Float)
    # 不可逆规范化指纹，用于本地候选人身份匹配（不承载唯一约束，历史数据可能重复）。
    phone_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    email_fingerprint: Mapped[str | None] = mapped_column(String(255), index=True)
    manual_fields: Mapped[list[str] | None] = mapped_column(JSON)
    candidate: Mapped[Candidate] = relationship(back_populates="contact")


class ResumeDocument(IdMixin, Base):
    __tablename__ = "resume_document"

    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate: Mapped[Candidate] = relationship(back_populates="documents")
    revisions: Mapped[list[ResumeRevision]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class Blob(IdMixin, Base):
    __tablename__ = "blob"

    content_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    suffix: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    reference_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revisions: Mapped[list[ResumeRevision]] = relationship(back_populates="blob")


class ResumeRevision(IdMixin, Base):
    __tablename__ = "resume_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','READY','FAILED')",
            name="ck_resume_revision_status",
        ),
        Index("ix_resume_revision_current", "document_id", "is_current"),
    )

    document_id: Mapped[str] = mapped_column(
        ForeignKey("resume_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    blob_id: Mapped[str] = mapped_column(ForeignKey("blob.id"), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    parsed_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parse_version: Mapped[str | None] = mapped_column(String(100))
    manual_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extraction_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    review_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    document: Mapped[ResumeDocument] = relationship(back_populates="revisions")
    blob: Mapped[Blob] = relationship(back_populates="revisions")


class ResumeImportClaim(IdMixin, Base):
    """数据库级导入幂等声明：同一 content_sha256 只能成功声明一次。"""

    __tablename__ = "resume_import_claim"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_resume_import_claim_sha"),
    )

    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)


class TaskRecord(IdMixin, Base):
    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','QUEUED','RUNNING','RETRY_WAIT','SUCCESS','FAILED','CANCELLED','DEAD_LETTER','PAUSED')",
            name="ck_task_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_task_idempotency_key"),
        Index("ix_task_claim", "status", "queue_name", "priority", "created_at"),
    )

    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    events: Mapped[list[TaskEvent]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )


class TaskEvent(IdMixin, Base):
    __tablename__ = "task_event"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    task: Mapped[TaskRecord] = relationship(back_populates="events")


class Jd(IdMixin, Base):
    __tablename__ = "jd"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','OPEN','PAUSED','FILLED','CANCELLED','ARCHIVED')",
            name="ck_jd_status",
        ),
        Index("ix_jd_active", "deleted_at", "status"),
    )

    company: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revisions: Mapped[list[JdRevision]] = relationship(
        back_populates="jd",
        cascade="all, delete-orphan",
    )


class JdRevision(IdMixin, Base):
    __tablename__ = "jd_revision"
    __table_args__ = (
        CheckConstraint(
            "ai_category IN ('CORE_AI','AI_RELATED','NON_AI') OR ai_category IS NULL",
            name="ck_jd_revision_ai_category",
        ),
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','READY','FAILED')",
            name="ck_jd_revision_status",
        ),
        Index("ix_jd_revision_current", "jd_id", "is_current"),
    )

    jd_id: Mapped[str] = mapped_column(
        ForeignKey("jd.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_text: Mapped[str | None] = mapped_column(Text)
    parsed_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ai_category: Mapped[str | None] = mapped_column(String(24))
    highest_degree: Mapped[str | None] = mapped_column(String(32))
    min_years: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    location: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manual_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    jd: Mapped[Jd] = relationship(back_populates="revisions")
    requirements: Mapped[list[JdRequirement]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
    )


class JdRequirement(IdMixin, Base):
    __tablename__ = "jd_requirement"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('MUST','PLUS','EXCLUDE')",
            name="ck_jd_requirement_kind",
        ),
    )

    revision_id: Mapped[str] = mapped_column(
        ForeignKey("jd_revision.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    revision: Mapped[JdRevision] = relationship(back_populates="requirements")


class CandidateJobCase(IdMixin, Base):
    __tablename__ = "candidate_job_case"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('待评估','待联系','已联系','有意向','已推荐','初试','复试','终试','Offer','入职','客户拒绝','候选人拒绝','暂缓','岗位关闭')",
            name="ck_case_stage",
        ),
        Index("ix_case_candidate", "candidate_id"),
        Index("ix_case_jd", "jd_id"),
        Index("ix_case_stage", "stage"),
    )

    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 岗位物理删除后置空（岗位快照保留），故可空 + SET NULL，不再级联删流程。
    jd_id: Mapped[str | None] = mapped_column(
        ForeignKey("jd.id", ondelete="SET NULL"),
        nullable=True,
    )
    stage: Mapped[str] = mapped_column(String(24), default="待评估", nullable=False)
    template_id: Mapped[str | None] = mapped_column(
        ForeignKey("hiring_process.id", ondelete="SET NULL"),
        index=True,
    )
    template_version: Mapped[int | None] = mapped_column(Integer)
    template_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # 候选人物理删除后的流程快照：候选人生删后 candidate_id 置空，流程历史仍可读。
    candidate_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    candidate_phone_snapshot_encrypted: Mapped[str | None] = mapped_column(Text)
    candidate_email_snapshot_encrypted: Mapped[str | None] = mapped_column(Text)
    candidate_profile_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    candidate_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 岗位物理删除后的流程快照：岗位删后 jd_id 置空，流程、轮次与面试记录仍可读。
    jd_title_snapshot: Mapped[str | None] = mapped_column(String(200))
    jd_company_snapshot: Mapped[str | None] = mapped_column(String(200))
    jd_profile_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    jd_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidate: Mapped[Candidate | None] = relationship()
    jd: Mapped[Jd | None] = relationship()
    stage_events: Mapped[list[StageEvent]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )
    rounds: Mapped[list[CaseRound]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[CaseEvent]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )


class StageEvent(IdMixin, Base):
    """Legacy stage-transition rows kept for backward compatibility.

    New business facts are recorded as :class:`CaseEvent`; this table is no
    longer written to and only retained so historical databases stay readable.
    """

    __tablename__ = "stage_event"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('待评估','待联系','已联系','有意向','已推荐','初试','复试','终试','Offer','入职','客户拒绝','候选人拒绝','暂缓','岗位关闭')",
            name="ck_stage_event_stage",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('推进','淘汰','offer','入职','拒接')",
            name="ck_stage_event_result",
        ),
    )

    case_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_job_case.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    round_no: Mapped[int | None] = mapped_column(Integer)
    round_name: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    case: Mapped[CandidateJobCase] = relationship(back_populates="stage_events")


class HiringProcess(IdMixin, Base):
    """Per-JD interview-round template.

    A company-level default is derived by looking up other JDs with the same
    ``Jd.company`` string, so no separate company template entity is needed.
    """

    __tablename__ = "hiring_process"

    jd_id: Mapped[str] = mapped_column(
        ForeignKey("jd.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rounds: Mapped[list[ProcessRound]] = relationship(
        back_populates="process",
        cascade="all, delete-orphan",
    )


class ProcessRound(IdMixin, Base):
    __tablename__ = "process_round"
    __table_args__ = (
        UniqueConstraint("process_id", "round_no", name="uq_process_round_no"),
    )

    process_id: Mapped[str] = mapped_column(
        ForeignKey("hiring_process.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    round_name: Mapped[str] = mapped_column(String(64), nullable=False)
    round_type: Mapped[str | None] = mapped_column(String(32))
    process: Mapped[HiringProcess] = relationship(back_populates="rounds")


class CaseRound(IdMixin, Base):
    """A stable interview-round instance on a specific case.

    ``id`` is the stable round identity; ``round_no``/``sort_order`` are only
    for display ordering and never used to infer pass/fail. Ad-hoc retries are
    new rows (same ``round_no``, new ``id``), while rescheduling only mutates
    the related event's ``occurred_at``.
    """

    __tablename__ = "case_round"
    __table_args__ = (
        Index("ix_case_round_case", "case_id"),
    )

    case_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_job_case.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    round_name: Mapped[str] = mapped_column(String(64), nullable=False)
    round_type: Mapped[str | None] = mapped_column(String(32))
    definition_key: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="template", nullable=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    case: Mapped[CandidateJobCase] = relationship(back_populates="rounds")
    events: Mapped[list[CaseEvent]] = relationship(back_populates="round")


class CaseEvent(IdMixin, Base):
    """Append-only business facts driving the recruitment pipeline and dashboard.

    ``occurred_at`` is the business time (editable for backfill), ``recorded_at``
    is the system ingestion time. ``idempotency_key`` deduplicates retries and
    ``supersedes_event_id`` links a correction to the event it voids.
    """

    __tablename__ = "case_event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('RECOMMENDED','INTERVIEW_ENTERED','INTERVIEW_RESULT',"
            "'OFFER','ONBOARDED','EXIT','NOTE')",
            name="ck_case_event_type",
        ),
        CheckConstraint(
            "status IN ('active','void')",
            name="ck_case_event_status",
        ),
        Index("ix_case_event_case", "case_id"),
        Index("ix_case_event_occurred", "occurred_at"),
        Index("ix_case_event_type", "event_type"),
    )

    case_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_job_case.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    case_round_id: Mapped[str | None] = mapped_column(
        ForeignKey("case_round.id", ondelete="CASCADE"),
        index=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    result: Mapped[str | None] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    supersedes_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("case_event.id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(String(12), default="active", nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    case: Mapped[CandidateJobCase] = relationship(back_populates="events")
    round: Mapped[CaseRound | None] = relationship(back_populates="events")


class MatchRun(IdMixin, Base):
    __tablename__ = "match_run"

    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    jd_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("jd_revision.id", ondelete="SET NULL"),
        index=True,
    )
    query_text: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(String(16))
    index_generation: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    results: Mapped[list[MatchResult]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class MatchResult(IdMixin, Base):
    __tablename__ = "match_result"
    __table_args__ = (
        CheckConstraint(
            "status IN ('未处理','保留')",
            name="ck_match_result_status",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("match_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate_job_case.id", ondelete="SET NULL")
    )
    resume_revision_id: Mapped[str | None] = mapped_column(String(36))
    jd_revision_id: Mapped[str | None] = mapped_column(String(36))
    total_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="未处理", nullable=False)
    run: Mapped[MatchRun] = relationship(back_populates="results")


class SearchReview(IdMixin, Base):
    """搜索侧 AI 复核结论：按「查询指纹 + 候选人」可查。

    输入是用户的搜索条件（不是 JD），所以不挂在 match run 上；结论独立落表，
    同一个组合重复复核直接覆盖，界面按当前结果集逐行取用。
    """

    __tablename__ = "search_review"
    __table_args__ = (
        UniqueConstraint("query_key", "candidate_id", name="uq_search_review_query_candidate"),
    )

    query_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conditions_text: Mapped[str | None] = mapped_column(Text)
    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    revision_id: Mapped[str | None] = mapped_column(String(36))
    verdict: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    highlights: Mapped[list[str] | None] = mapped_column(JSON)
    risks: Mapped[list[str] | None] = mapped_column(JSON)
    # 判据依据（不进 UI）：来源、是否被一致性兜底降级等，供排查「为什么判成待核」。
    basis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(String(80))


class CorrectionLog(IdMixin, Base):
    __tablename__ = "correction_log"

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    reverted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MappingProject(IdMixin, Base):
    __tablename__ = "mapping_project"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    snapshots: Mapped[list[MappingSnapshot]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )


class MappingSnapshot(IdMixin, Base):
    __tablename__ = "mapping_snapshot"
    __table_args__ = (
        Index("ix_mapping_snapshot_current", "project_id", "is_current"),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("mapping_project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    project: Mapped[MappingProject] = relationship(back_populates="snapshots")
    nodes: Mapped[list[MappingNode]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class MappingNode(IdMixin, Base):
    __tablename__ = "mapping_node"

    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("mapping_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("mapping_node.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    snapshot: Mapped[MappingSnapshot] = relationship(back_populates="nodes")
    children: Mapped[list[MappingNode]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys=[parent_id],
    )
    parent: Mapped[MappingNode | None] = relationship(
        back_populates="children",
        remote_side="MappingNode.id",
    )


class MailCursor(IdMixin, Base):
    __tablename__ = "mail_cursor"

    mailbox: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    last_uid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # IMAP UIDVALIDITY：服务商重排 UID 时会变化，用于检测旧游标是否失效。
    uidvalidity: Mapped[int | None] = mapped_column(Integer)


class MailProcessRecord(IdMixin, Base):
    """单封邮件的处理状态，与扫描游标分离，用于可靠重试。

    - pending：待投递（已通过白名单、尚未入库成功）。
    - succeeded：已成功入库并标记已读。
    - failed：入库失败，等待重试（attempts 递增）。
    - ignored：非白名单或不含简历，不标已读但也不重复处理。
    """

    __tablename__ = "mail_process_record"
    __table_args__ = (
        Index("ix_mail_process_lookup", "mailbox", "uidvalidity", "uid", unique=True),
    )

    mailbox: Mapped[str] = mapped_column(String(200), nullable=False)
    uidvalidity: Mapped[int | None] = mapped_column(Integer)
    uid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class Reminder(IdMixin, Base):
    __tablename__ = "reminder"
    __table_args__ = (
        Index("ix_reminder_due", "remind_at", "dismissed"),
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("candidate_job_case.id", ondelete="CASCADE"), index=True)
    paused_by_workflow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    time_basis: Mapped[str] = mapped_column(String(24), default="UTC", nullable=False)


class CandidateReminder(IdMixin, Base):
    """候选人提醒：使用者在人才库某一行建立的自定义待办。

    与 ``Reminder`` 是两套东西——后者挂在招聘流程上、有到期时间、到期会发邮件、
    且会被流程终态暂停。候选人提醒**没有日期**，建好即出现在「今日待办」，
    勾选表示**任务完成**并移出列表（与系统项「今天处理过、次日重置」不同）。

    ``candidate_name_snapshot`` 用于「固定显示人名」：候选人改名或删除后，
    提醒行仍显示建立时的人名。
    """
    __tablename__ = "candidate_reminder"
    __table_args__ = (
        Index("ix_candidate_reminder_open", "candidate_id", "done"),
    )

    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexSyncRecord(IdMixin, Base):
    """Durable outbox: revisions coalesce per entity, failures remain retryable."""
    __tablename__ = "index_sync"
    __table_args__ = (UniqueConstraint("entity_type", "entity_id", name="uq_index_sync_entity"),)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(16), default="FULL", nullable=False)
    requested_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    applied_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BdLead(IdMixin, Base):
    __tablename__ = "bd_lead"
    __table_args__ = (
        CheckConstraint(
            "status IN ('新线索','已联系','已定级','已存档')",
            name="ck_bd_lead_status",
        ),
        Index("ix_bd_lead_status", "status"),
    )

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[str | None] = mapped_column(String(500), index=True)
    company_name: Mapped[str] = mapped_column(String(500), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(500))
    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    raw_snippet: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="新线索", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    confidence: Mapped[float | None] = mapped_column(Float)
    is_hiring: Mapped[bool | None] = mapped_column(Boolean)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("bd_search_session.id", ondelete="SET NULL"),
        index=True,
    )
    synthesized_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    posted_time: Mapped[str | None] = mapped_column(String(100))
    salary_range: Mapped[str | None] = mapped_column(String(100))
    level: Mapped[str | None] = mapped_column(String(100))
    requirements: Mapped[list[str] | None] = mapped_column(JSON)
    session: Mapped[BdSearchSession | None] = relationship(back_populates="leads")
    evidence: Mapped[list[BdEvidence]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
    )


class BdSearchSession(IdMixin, Base):
    __tablename__ = "bd_search_session"

    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="completed", nullable=False)
    leads: Mapped[list[BdLead]] = relationship(back_populates="session")


class BdEvidence(IdMixin, Base):
    __tablename__ = "bd_evidence"

    lead_id: Mapped[str] = mapped_column(
        ForeignKey("bd_lead.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    claim: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    lead: Mapped[BdLead] = relationship(back_populates="evidence")


class Company(IdMixin, Base):
    __tablename__ = "company"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_text: Mapped[str | None] = mapped_column(Text)
    departments: Mapped[list[Department]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    employees: Mapped[list[Employee]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )


class Department(IdMixin, Base):
    __tablename__ = "department"
    __table_args__ = (
        Index("ix_department_company_parent", "company_id", "parent_id"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("department.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    leader_id: Mapped[str | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL", use_alter=True, name="fk_department_leader"),
    )
    leader_report_to: Mapped[str | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL", use_alter=True, name="fk_department_leader_report_to"),
    )
    team_size: Mapped[int | None] = mapped_column(Integer)
    business_direction: Mapped[str | None] = mapped_column(Text)
    tech_stack: Mapped[str | None] = mapped_column(Text)
    office_location: Mapped[str | None] = mapped_column(String(64))
    hc_status: Mapped[str | None] = mapped_column(String(32))
    hc_internal_note: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship(back_populates="departments")
    parent: Mapped[Department | None] = relationship(
        remote_side="Department.id",
        back_populates="children",
    )
    children: Mapped[list[Department]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    employees: Mapped[list[Employee]] = relationship(
        back_populates="department",
        cascade="all, delete-orphan",
        foreign_keys="Employee.department_id",
    )


class Employee(IdMixin, Base):
    __tablename__ = "employee"
    __table_args__ = (
        Index("ix_employee_department", "company_id", "department_id"),
    )

    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    department_id: Mapped[str | None] = mapped_column(
        ForeignKey("department.id", ondelete="CASCADE"),
        index=True,
    )
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="SET NULL"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    job_level: Mapped[str | None] = mapped_column(String(32))
    report_to: Mapped[str | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"),
        index=True,
    )
    subordinate_count: Mapped[int | None] = mapped_column(Integer)
    tenure_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    business_module: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    intention: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    phone_encrypted: Mapped[str | None] = mapped_column(Text)
    is_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    company: Mapped[Company] = relationship(back_populates="employees")
    department: Mapped[Department | None] = relationship(
        back_populates="employees",
        foreign_keys=[department_id],
    )
    report_to_employee: Mapped[Employee | None] = relationship(
        remote_side="Employee.id",
        back_populates="subordinates",
    )
    subordinates: Mapped[list[Employee]] = relationship(
        back_populates="report_to_employee",
        foreign_keys=[report_to],
    )


class DailyFollowupState(IdMixin, Base):
    """每日待跟进报告的发送状态（单行记录，记录最后发送的早晚日期）。"""
    __tablename__ = "daily_followup_state"

    last_evening_date: Mapped[str | None] = mapped_column(String(10))
    last_morning_date: Mapped[str | None] = mapped_column(String(10))


class DailyTodoCheck(IdMixin, Base):
    """「今日待办」系统项的当日勾选记录。

    勾选只表示「使用者今天处理过这一项」，**不代表任务完成**：记录带日期，
    次日按当天日期查询自然查不到，即回到未勾选——每日重置因此不需要任何定时任务。

    与 ``candidate_reminder`` 的勾选语义不同：后者的勾选是「任务完成」，永久生效。
    """
    __tablename__ = "daily_todo_check"
    __table_args__ = (
        UniqueConstraint("check_date", "item_key", name="uq_daily_todo_check_date_item"),
    )

    check_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(120), nullable=False)


class School(IdMixin, Base):
    """学校参考映射：标准名/别名 + 重叠标签（985/211/双一流/普本/大专/海外）+ QS 排名区间。"""

    __tablename__ = "school"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_school_normalized_name"),
        Index("ix_school_canonical_name", "canonical_name"),
        Index("ix_school_source", "source"),
    )

    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(JSON)
    country_region: Mapped[str | None] = mapped_column(String(64))
    institution_type: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    qs_year: Mapped[int | None] = mapped_column(Integer)
    qs_rank_start: Mapped[int | None] = mapped_column(Integer)
    qs_rank_end: Mapped[int | None] = mapped_column(Integer)
    rank_display: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="builtin", nullable=False)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
