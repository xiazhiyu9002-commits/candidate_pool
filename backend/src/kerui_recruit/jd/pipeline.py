from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import JdRevision, JdRequirement
from kerui_recruit.jd.profile_constraints import merge_rule_constraints
from kerui_recruit.jd.structured import ExactConstraint, JdParser
from kerui_recruit.search.sync import enqueue_sync


@dataclass(frozen=True, slots=True)
class JdPipelineResult:
    jd_id: str
    revision_id: str
    status: str


class JdPipeline:
    def __init__(self, *, session_factory: sessionmaker[Session], parser: JdParser) -> None:
        self.session_factory = session_factory
        self.parser = parser

    async def run(self, revision_id: str) -> JdPipelineResult:
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            revision = session.get(JdRevision, revision_id)
            if revision is None:
                raise LookupError(f"Jd revision not found: {revision_id}")
            source_text = revision.source_text
            previous_ready = revision.status == "READY"
            revision.status = "PROCESSING"
            session.commit()

        try:
            # Never hold the SQLite write lock across an external provider await.
            parsed = await self.parser.parse_jd(source_text or "")
            # 模型 ∪ 规则：JD 导入此前只跑模型，明写的学历 / 年限 / 公司背景会随模型抖动丢失
            # （实测同一份多段 JD 两次导入，一次有 degree 一次没有）。这里在唯一入口补齐，
            # 远端 / 本地 / 视觉三种解析器共用同一套兜底。
            merged, rule_years = merge_rule_constraints(parsed.exact_constraints, source_text)
            parsed.exact_constraints = [ExactConstraint(**item) for item in merged]
            # 年限只在模型没给出时才用规则兜底——模型的判断通常更细，不覆盖它。
            if parsed.min_years is None and rule_years is not None:
                parsed.min_years = rule_years
            with self.session_factory() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                revision = session.get(JdRevision, revision_id)
                if revision is None:
                    raise LookupError(f"Jd revision not found: {revision_id}")
                if revision.source_text != source_text or revision.status != "PROCESSING":
                    raise RuntimeError("JD source or state changed during parsing; result discarded")
                data = parsed.model_dump(mode="json")
                # 双形态画像：整体段落（candidate_profile）同时作为 narrative，分点/浓缩由解析器同源产出。
                if data.get("candidate_profile") and not data.get("candidate_profile_narrative"):
                    data["candidate_profile_narrative"] = data["candidate_profile"]
                # 人工编辑（如 candidate_profile）优先于自动解析，重新解析不得覆盖。
                data.update(dict(revision.manual_overrides or {}))
                # Explicit/imported titles and companies are authoritative. A
                # parser may fill missing labels, but cannot erase human edits.
                if revision.is_current:
                    revision.jd.company = revision.jd.company or parsed.company
                    revision.jd.title = revision.jd.title or parsed.title
                    data.update(company=revision.jd.company, title=revision.jd.title)
                revision.parsed_data = data
                revision.ai_category = parsed.ai_category
                revision.highest_degree = parsed.highest_degree
                revision.min_years = None if parsed.min_years is None else _decimal(parsed.min_years)
                revision.location = parsed.location
                revision.requirements = [
                    JdRequirement(kind=req.kind, label=req.label, value=req.value)
                    for req in parsed.requirements
                ]
                revision.status = "READY"
                enqueue_sync(session, "jd", revision.jd_id)
                session.commit()
                return JdPipelineResult(jd_id=revision.jd_id, revision_id=revision.id, status="READY")
        except BaseException:
            with self.session_factory() as session, session.begin():
                revision = session.get(JdRevision, revision_id)
                if revision is not None and revision.status == "PROCESSING" and revision.source_text == source_text:
                    revision.status = "READY" if previous_ready else "FAILED"
                    enqueue_sync(session, "jd", revision.jd_id)
            raise

    def _source_text(self, revision_id: str) -> str:
        with self.session_factory() as session:
            revision = session.get(JdRevision, revision_id)
            if revision is None:
                raise LookupError(f"Jd revision not found: {revision_id}")
            return revision.source_text or ""

    async def split(self, text: str) -> list[str]:
        """Split a possibly multi-JD text blob into individual JD chunks."""
        return await self.parser.split_jds(text)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
