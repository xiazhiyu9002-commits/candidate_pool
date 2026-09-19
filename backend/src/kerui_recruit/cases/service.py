from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import (
    Candidate,
    CandidateJobCase,
    CaseEvent,
    CaseRound,
    HiringProcess,
    Jd,
    ProcessRound,
    Reminder,
)
from kerui_recruit.cases.projection import effective_events
from kerui_recruit.search.sync import enqueue_sync

# 业务时区：统计与「默认日期」口径统一使用 Asia/Shanghai。
SHANGHAI = timezone(timedelta(hours=8))

# 面试轮结果（区分「已判定」与「未判定」，用于通过率分母）。
RESULT_PASS = "通过"
RESULT_FAIL = "未通过"
RESULT_PENDING = "待反馈"
RESULT_SKIPPED = "跳过"
RESULT_CANDIDATE_EXIT = "候选人退出"
RESULT_CANCELLED = "取消"
INTERVIEW_RESULTS = (
    RESULT_PENDING,
    RESULT_PASS,
    RESULT_FAIL,
    RESULT_SKIPPED,
    RESULT_CANDIDATE_EXIT,
    RESULT_CANCELLED,
)

# Offer 结果。
OFFER_ISSUED = "已发放"
OFFER_ACCEPTED = "已接受"
OFFER_REJECTED = "已拒绝"
OFFER_WITHDRAWN = "已撤回"
OFFER_ONBOARDED = "已入职"


class CaseStateError(ValueError):
    """A business transition is not allowed for the current case state."""


def _now() -> datetime:
    # 统一以 naive UTC 存储，避免 SQLite 丢失时区导致的统计偏差。
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _coerce_occurred(dt: datetime | None) -> datetime:
    """把用户录入时间规范为 naive UTC；无时区的视为 Asia/Shanghai。"""
    if dt is None:
        return _now()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI).astimezone(timezone.utc).replace(tzinfo=None)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _interview_stage(round_no: int) -> str:
    """面试阶段按轮次映射，保证内外显示一致：1→初试，2→复试，3+→终试。"""
    return {1: "初试", 2: "复试"}.get(round_no, "终试")


class CaseService:
    """事件驱动的人岗招聘流程。

    业务事实以 :class:`CaseEvent` 追加记录，``occurred_at`` 为业务时间、
    ``recorded_at`` 为系统录入时间；``idempotency_key`` 去重重复请求。
    轮次实例 :class:`CaseRound` 提供稳定 round_id，顺序仅用于展示。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------------ 模板
    def get_process(self, jd_id: str) -> list[dict]:
        with self.session_factory() as session:
            return self._load_process_rounds(session, jd_id)

    def set_process(self, jd_id: str, rounds: list[dict]) -> list[dict]:
        """设置岗位模板；模板修改只增加 version，不追溯覆盖已有 case。"""
        with self.session_factory() as session, session.begin():
            target_jd = session.get(Jd, jd_id)
            if target_jd is None:
                raise LookupError(f"JD not found: {jd_id}")
            # v7 introduced snapshots.  Rows upgraded from v6 remain NULL
            # until first use, so changing the source template first would
            # silently rewrite their remaining interviews. Freeze every NULL
            # case whose direct or inherited company template may be affected.
            legacy_cases = session.scalars(
                select(CandidateJobCase)
                .join(Jd, CandidateJobCase.jd_id == Jd.id)
                .where(
                    CandidateJobCase.template_snapshot.is_(None),
                    or_(CandidateJobCase.jd_id == jd_id, Jd.company == target_jd.company),
                )
            ).all()
            for legacy_case in legacy_cases:
                self._capture_template(session, legacy_case)
            process = session.scalars(
                select(HiringProcess).where(HiringProcess.jd_id == jd_id)
            ).one_or_none()
            if process is None:
                process = HiringProcess(jd_id=jd_id, version=1)
                session.add(process)
                session.flush()
            else:
                process.version += 1
            for existing_round in list(process.rounds):
                session.delete(existing_round)
            session.flush()

            for item in sorted(rounds, key=lambda r: int(r["round_no"])):
                session.add(
                    ProcessRound(
                        process_id=process.id,
                        round_no=int(item["round_no"]),
                        round_name=str(item["round_name"]),
                        round_type=item.get("round_type"),
                    )
                )
        return self.get_process(jd_id)

    def _load_process_rounds(self, session: Session, jd_id: str) -> list[dict]:
        process = session.scalars(
            select(HiringProcess).where(HiringProcess.jd_id == jd_id)
        ).one_or_none()
        if process is None:
            return []
        rounds = session.scalars(
            select(ProcessRound)
            .where(ProcessRound.process_id == process.id)
            .order_by(ProcessRound.round_no.asc())
        ).all()
        return [
            {"round_no": r.round_no, "round_name": r.round_name, "round_type": r.round_type}
            for r in rounds
        ]

    def _resolve_template_rounds(self, session: Session, case: CandidateJobCase) -> list[dict]:
        """岗位模板优先；其次复用同公司其他岗位的模板。"""
        if case.template_snapshot is not None:
            return case.template_snapshot
        rounds = self._load_process_rounds(session, case.jd_id)
        if rounds:
            return rounds
        jd = session.get(Jd, case.jd_id)
        if jd is None:
            return []
        process = session.scalars(
            select(HiringProcess)
            .join(Jd, HiringProcess.jd_id == Jd.id)
            .where(Jd.company == jd.company, HiringProcess.jd_id != case.jd_id)
            .order_by(HiringProcess.created_at.asc())
        ).first()
        if process is None:
            return []
        return [
            {"round_no": r.round_no, "round_name": r.round_name, "round_type": r.round_type}
            for r in sorted(process.rounds, key=lambda r: r.round_no)
        ]

    # ------------------------------------------------------------------ 流程
    def create(self, *, candidate_id: str, jd_id: str, note: str | None = None) -> CandidateJobCase:
        with self.session_factory() as session, session.begin():
            existing = session.scalars(
                select(CandidateJobCase).where(
                    CandidateJobCase.candidate_id == candidate_id,
                    CandidateJobCase.jd_id == jd_id,
                    CandidateJobCase.deleted_at.is_(None),
                )
            ).one_or_none()
            if existing is not None:
                return existing
            case = CandidateJobCase(candidate_id=candidate_id, jd_id=jd_id, stage="待评估", note=note)
            self._ensure_open(session, case)
            session.add(case)
            session.flush()
            self._capture_template(session, case)
            return case

    def _capture_template(self, session: Session, case: CandidateJobCase) -> None:
        if case.template_snapshot is not None:
            return
        jd = session.get(Jd, case.jd_id)
        process = session.scalar(select(HiringProcess).where(
            HiringProcess.jd_id == case.jd_id, HiringProcess.deleted_at.is_(None)))
        if process is None and jd is not None:
            process = session.scalar(select(HiringProcess).join(Jd).where(
                Jd.company == jd.company, Jd.deleted_at.is_(None), HiringProcess.deleted_at.is_(None))
                .order_by(HiringProcess.created_at, HiringProcess.id))
        case.template_snapshot = [] if process is None else [
            {"round_no": r.round_no, "round_name": r.round_name, "round_type": r.round_type}
            for r in sorted(process.rounds, key=lambda r: r.round_no)]
        case.template_id = process.id if process else None
        case.template_version = process.version if process else None

    @staticmethod
    def _ensure_open(session: Session, case: CandidateJobCase) -> None:
        jd = session.get(Jd, case.jd_id)
        candidate = session.get(Candidate, case.candidate_id)
        if case.deleted_at or jd is None or jd.deleted_at or jd.status != "OPEN":
            raise CaseStateError("岗位未开放、已关闭或删除，不能继续推进")
        if candidate is None or candidate.deleted_at or candidate.status != "AVAILABLE":
            raise CaseStateError("候选人不可推荐、待复核或已删除，不能继续推进")
        if CaseService._has_active_onboarding(session, case.candidate_id):
            raise CaseStateError("候选人已有有效入职记录，不能继续推荐或推进")
        if case.stage in ("入职", "候选人拒绝", "客户拒绝", "岗位关闭"):
            raise CaseStateError("流程已结束，请先核对并纠正历史记录")

    @staticmethod
    def _has_active_onboarding(session: Session, candidate_id: str) -> bool:
        return session.scalar(
            select(CandidateJobCase.id)
            .join(CaseEvent, CaseEvent.case_id == CandidateJobCase.id)
            .where(
                CandidateJobCase.candidate_id == candidate_id,
                CandidateJobCase.deleted_at.is_(None),
                CaseEvent.status == "active",
                or_(
                    CaseEvent.event_type == "ONBOARDED",
                    and_(CaseEvent.event_type == "OFFER", CaseEvent.result == OFFER_ONBOARDED),
                ),
            )
            .limit(1)
        ) is not None

    @staticmethod
    def _key(case_id: str, action: str, key: str | None) -> str | None:
        return sha256(f"{case_id}:{action}:{key}".encode()).hexdigest() if key else None

    def get(self, case_id: str) -> CandidateJobCase:
        with self.session_factory() as session:
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            return case

    def list_cases(
        self,
        *,
        candidate_id: str | None = None,
        jd_id: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> list[CandidateJobCase]:
        with self.session_factory() as session:
            stmt = select(CandidateJobCase).where(CandidateJobCase.deleted_at.is_(None))
            if candidate_id is not None:
                stmt = stmt.where(CandidateJobCase.candidate_id == candidate_id)
            if jd_id is not None:
                stmt = stmt.where(CandidateJobCase.jd_id == jd_id)
            stmt = stmt.order_by(CandidateJobCase.created_at.desc())
            if offset is not None:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt).all())

    def count_cases(
        self,
        *,
        candidate_id: str | None = None,
        jd_id: str | None = None,
    ) -> int:
        with self.session_factory() as session:
            stmt = select(func.count()).select_from(CandidateJobCase).where(
                CandidateJobCase.deleted_at.is_(None))
            if candidate_id is not None:
                stmt = stmt.where(CandidateJobCase.candidate_id == candidate_id)
            if jd_id is not None:
                stmt = stmt.where(CandidateJobCase.jd_id == jd_id)
            return int(session.scalar(stmt) or 0)

    def delete(self, case_id: str) -> None:
        """软删除流程：置 deleted_at 并从列表/看板隐藏，历史事件与轮次保留。"""
        with self.session_factory() as session, session.begin():
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            case.deleted_at = _now()
            self._sync_candidate_and_reminders(session, case)

    # ------------------------------------------------------------ 业务动作
    def recommend(
        self,
        case_id: str,
        *,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """记录一次明确推荐。同 key 幂等，不重复计推荐。"""
        return self._append_event(
            case_id,
            event_type="RECOMMENDED",
            result=None,
            occurred_at=occurred_at,
            note=note,
            idempotency_key=idempotency_key,
            stage="已推荐",
        )

    def enter_interview(
        self,
        case_id: str,
        *,
        round_name: str | None = None,
        round_type: str | None = None,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """进入下一轮面试：创建/复用轮次实例并记录进入事件。"""
        idempotency_key = self._key(case_id, "enter", idempotency_key)
        with self.session_factory() as session, session.begin():
            existing = self._dedupe(session, idempotency_key)
            if existing is not None:
                return existing
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            self._ensure_open(session, case)
            current_round, result = self._current_interview(session, case)
            if current_round is not None and result not in (RESULT_PASS, RESULT_SKIPPED):
                raise CaseStateError("当前轮次仍待反馈或结果未通过，不能重复进入下一轮")
            next_no, name, rtype = self._next_round(session, case, round_name, round_type)
            case_round = CaseRound(
                case_id=case_id,
                round_no=next_no,
                round_name=name,
                round_type=rtype,
                sort_order=next_no,
                source="ad_hoc" if round_name else "template",
                definition_key=self._round_key(case, next_no, name, rtype, round_name is not None),
            )
            session.add(case_round)
            session.flush()
            event = self._add_event(
                session,
                case,
                "INTERVIEW_ENTERED",
                case_round_id=case_round.id,
                result=None,
                occurred_at=occurred_at,
                note=note,
                idempotency_key=idempotency_key,
            )
            self._sync_stage(session, case_id)
            return event

    def record_result(
        self,
        case_id: str,
        *,
        case_round_id: str,
        result: str,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """记录某轮结果（通过/未通过/待反馈/跳过/候选人退出/取消）。"""
        if result not in INTERVIEW_RESULTS:
            raise CaseStateError(f"Unknown interview result: {result}")
        return self._append_round_event(
            case_id,
            case_round_id,
            "INTERVIEW_RESULT",
            result,
            occurred_at,
            note,
            idempotency_key,
        )

    def pass_and_advance(
        self,
        case_id: str,
        *,
        case_round_id: str,
        next_round_name: str | None = None,
        next_round_type: str | None = None,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> list[CaseEvent]:
        """「通过并进入下一轮」：同一事务记录「通过」与「进入下一轮」两件事实。"""
        idempotency_key = self._key(case_id, f"pass:{case_round_id}", idempotency_key)
        result_key = self._key(case_id, "pass-result", idempotency_key)
        with self.session_factory() as session, session.begin():
            existing = self._dedupe(session, idempotency_key)
            if existing is not None:
                passed = self._dedupe(session, result_key)
                if passed is None:
                    # Older calls stored only the entered event's key. Recover its
                    # preceding result by the same round/time, without creating data.
                    passed = session.scalar(select(CaseEvent).where(
                        CaseEvent.case_id == case_id, CaseEvent.case_round_id == case_round_id,
                        CaseEvent.event_type == "INTERVIEW_RESULT", CaseEvent.result == RESULT_PASS,
                        CaseEvent.occurred_at <= existing.occurred_at,
                        CaseEvent.recorded_at <= existing.recorded_at,
                    ).order_by(CaseEvent.recorded_at.desc(), CaseEvent.id.desc()).limit(1))
                return [passed, existing] if passed is not None else [existing]
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            self._ensure_open(session, case)
            case_round = session.get(CaseRound, case_round_id)
            if case_round is None or case_round.case_id != case_id:
                raise LookupError(f"CaseRound not found: {case_round_id}")

            current_round, _ = self._current_interview(session, case)
            if current_round is None or current_round.id != case_round_id:
                raise CaseStateError("只能推进当前有效轮次，旧轮次或已作废轮次不能再次推进")
            next_no, name, rtype = self._next_round(session, case, next_round_name, next_round_type)

            passed = self._add_event(
                session, case, "INTERVIEW_RESULT", case_round_id=case_round_id,
                result=RESULT_PASS, occurred_at=occurred_at, note=note,
                idempotency_key=result_key,
            )
            next_round = CaseRound(
                case_id=case_id, round_no=next_no, round_name=name,
                round_type=rtype, sort_order=next_no,
                source="ad_hoc" if next_round_name else "template",
                definition_key=self._round_key(case, next_no, name, rtype, next_round_name is not None),
            )
            session.add(next_round)
            session.flush()
            entered = self._add_event(
                session, case, "INTERVIEW_ENTERED", case_round_id=next_round.id,
                result=None, occurred_at=occurred_at, note=None,
                idempotency_key=idempotency_key,
            )
            self._sync_stage(session, case_id)
            return [passed, entered]

    def offer(
        self,
        case_id: str,
        *,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """记录一次明确的 Offer 发放事实。"""
        return self._append_event(
            case_id, "OFFER", OFFER_ISSUED, occurred_at, note, idempotency_key, stage="Offer",
        )

    def update_offer(
        self,
        case_id: str,
        *,
        result: str,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """更新 Offer 状态（已接受/已拒绝/已撤回/已入职）。"""
        if result not in (OFFER_ACCEPTED, OFFER_REJECTED, OFFER_WITHDRAWN, OFFER_ONBOARDED):
            raise CaseStateError(f"Unknown offer result: {result}")
        stage = "入职" if result == OFFER_ONBOARDED else "Offer"
        return self._append_event(
            case_id, "OFFER", result, occurred_at, note, idempotency_key, stage=stage,
        )

    def onboard(
        self,
        case_id: str,
        *,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        return self._append_event(
            case_id, "ONBOARDED", None, occurred_at, note, idempotency_key, stage="入职",
        )

    def exit(
        self,
        case_id: str,
        *,
        result: str | None = None,
        occurred_at: datetime | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> CaseEvent:
        """候选人退出/客户淘汰。``result`` 区分『候选人退出』与客户淘汰。"""
        stage = "候选人拒绝" if result == RESULT_CANDIDATE_EXIT else "客户拒绝"
        return self._append_event(
            case_id, "EXIT", result, occurred_at, note, idempotency_key, stage=stage,
        )

    # ------------------------------------------------------------------ 纠错
    def void_event(self, event_id: str, *, note: str | None = None) -> str:
        """纠错/撤回：直接删除该事件，不保留作废记录，随后重新同步状态。

        作废「进入面试」会连同其面试轮次一起删除（该轮的结果随之移除），
        其余事件只删除自身。删除后按剩余有效事实重算阶段。
        """
        with self.session_factory() as session, session.begin():
            event = session.get(CaseEvent, event_id)
            if event is None:
                raise LookupError(f"CaseEvent not found: {event_id}")
            case_id = event.case_id
            if event.event_type == "INTERVIEW_ENTERED" and event.case_round_id:
                case_round = session.get(CaseRound, event.case_round_id)
                if case_round is not None:
                    for linked in session.scalars(
                        select(CaseEvent).where(CaseEvent.case_round_id == case_round.id)
                    ).all():
                        session.delete(linked)
                    session.delete(case_round)
            else:
                session.delete(event)
            session.flush()
            self._sync_stage(session, case_id)
            return event_id

    def get_timeline(self, case_id: str) -> list[CaseEvent]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(CaseEvent)
                    .where(CaseEvent.case_id == case_id)
                    .order_by(CaseEvent.occurred_at.asc(), CaseEvent.recorded_at.asc())
                ).all()
            )

    def get_rounds(self, case_id: str) -> list[CaseRound]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(CaseRound)
                    .where(CaseRound.case_id == case_id)
                    .order_by(CaseRound.sort_order.asc(), CaseRound.created_at.asc())
                ).all()
            )

    # ------------------------------------------------------------ 内部实现
    @staticmethod
    def _current_interview(session: Session, case: CandidateJobCase) -> tuple[CaseRound | None, str | None]:
        events = effective_events(list(session.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id))))
        entered = {event.case_round_id for event in events if event.event_type == "INTERVIEW_ENTERED"}
        current = session.scalar(select(CaseRound).where(CaseRound.id.in_(entered))
                                 .order_by(CaseRound.round_no.desc(), CaseRound.created_at.desc()).limit(1))
        if current is None:
            return None, None
        results = [event.result for event in events if event.event_type == "INTERVIEW_RESULT"
                   and event.case_round_id == current.id]
        return current, results[-1] if results else None

    def _next_round(
        self,
        session: Session,
        case: CandidateJobCase,
        round_name: str | None,
        round_type: str | None,
    ) -> tuple[int, str, str | None]:
        self._capture_template(session, case)
        active_rounds = select(CaseEvent.case_round_id).where(
            CaseEvent.case_id == case.id, CaseEvent.event_type == "INTERVIEW_ENTERED", CaseEvent.status == "active")
        existing = session.scalars(select(CaseRound).where(CaseRound.id.in_(active_rounds))).all()
        next_no = max([r.round_no for r in existing], default=0) + 1
        template = self._resolve_template_rounds(session, case)
        if template and next_no > len(template) and not (round_name and round_name.strip()):
            raise CaseStateError("当前已是最终轮，请记录通过结束面试；如需加面请明确填写轮次名称")
        if round_name is None and template and next_no <= len(template):
            item = template[next_no - 1]
            round_name = item["round_name"]
            round_type = round_type or item.get("round_type")
        if round_name is None:
            round_name = f"第{next_no}轮"
        return next_no, round_name, round_type

    @staticmethod
    def _round_key(case, number, name, round_type, ad_hoc):
        if not ad_hoc and case.template_id:
            return f"{case.template_id}:{case.template_version}:{number}"
        return f"custom:{number}:" + sha256(f"{name}:{round_type or ''}".encode()).hexdigest()[:20]

    def _append_event(
        self,
        case_id: str,
        event_type: str,
        result: str | None,
        occurred_at: datetime | None,
        note: str | None,
        idempotency_key: str | None,
        *,
        stage: str,
    ) -> CaseEvent:
        idempotency_key = self._key(case_id, f"{event_type}:{result}", idempotency_key)
        with self.session_factory() as session, session.begin():
            existing = self._dedupe(session, idempotency_key)
            if existing is not None:
                return existing
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            if event_type in ("RECOMMENDED", "ONBOARDED") or (event_type == "OFFER" and result in (OFFER_ISSUED, OFFER_ONBOARDED)):
                historical = False
                if occurred_at is not None and (event_type == "RECOMMENDED" or (event_type == "OFFER" and result == OFFER_ISSUED)):
                    facts = effective_events(list(session.scalars(select(CaseEvent).where(CaseEvent.case_id == case_id))))
                    historical = bool(facts and _coerce_occurred(occurred_at) < facts[-1].occurred_at)
                if historical:
                    # Strictly earlier facts can repair missing history without
                    # reopening a terminal case. Deleted entities remain protected.
                    jd = session.get(Jd, case.jd_id)
                    candidate = session.get(Candidate, case.candidate_id)
                    if case.deleted_at or jd is None or jd.deleted_at:
                        raise CaseStateError("岗位或流程已删除，不能补录历史")
                    if candidate is None or candidate.deleted_at:
                        raise CaseStateError("候选人已删除，不能补录历史")
                else:
                    self._ensure_open(session, case)
            event = self._add_event(
                session, case, event_type, case_round_id=None, result=result,
                occurred_at=occurred_at, note=note, idempotency_key=idempotency_key,
            )
            self._sync_stage(session, case_id)
            return event

    def _append_round_event(
        self,
        case_id: str,
        case_round_id: str,
        event_type: str,
        result: str,
        occurred_at: datetime | None,
        note: str | None,
        idempotency_key: str | None,
    ) -> CaseEvent:
        idempotency_key = self._key(case_id, f"{event_type}:{case_round_id}:{result}", idempotency_key)
        with self.session_factory() as session, session.begin():
            existing = self._dedupe(session, idempotency_key)
            if existing is not None:
                return existing
            case = session.get(CandidateJobCase, case_id)
            if case is None:
                raise LookupError(f"Case not found: {case_id}")
            case_round = session.get(CaseRound, case_round_id)
            if case_round is None or case_round.case_id != case_id:
                raise LookupError(f"CaseRound not found: {case_round_id}")
            event = self._add_event(
                session, case, event_type, case_round_id=case_round_id, result=result,
                occurred_at=occurred_at, note=note, idempotency_key=idempotency_key,
            )
            self._sync_stage(session, case_id)
            return event

    def _add_event(
        self,
        session: Session,
        case: CandidateJobCase,
        event_type: str,
        *,
        case_round_id: str | None,
        result: str | None,
        occurred_at: datetime | None,
        note: str | None,
        idempotency_key: str | None,
    ) -> CaseEvent:
        event = CaseEvent(
            case_id=case.id,
            event_type=event_type,
            case_round_id=case_round_id,
            occurred_at=_coerce_occurred(occurred_at),
            recorded_at=_now(),
            result=result,
            note=note,
            idempotency_key=idempotency_key,
            status="active",
        )
        session.add(event)
        session.flush()
        return event

    def _dedupe(self, session: Session, idempotency_key: str | None) -> CaseEvent | None:
        if not idempotency_key:
            return None
        return session.scalars(
            select(CaseEvent).where(CaseEvent.idempotency_key == idempotency_key)
        ).one_or_none()

    def _sync_stage(self, session: Session, case_id: str) -> None:
        case = session.get(CandidateJobCase, case_id)
        if case is None:
            return
        events = effective_events(list(session.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == case_id, CaseEvent.status == "active")
            .order_by(CaseEvent.occurred_at.asc(), CaseEvent.recorded_at.asc())
        ).all()))
        stage = "待评估"
        for event in events:
            if event.event_type == "RECOMMENDED":
                stage = "已推荐"
            elif event.event_type in ("INTERVIEW_ENTERED", "INTERVIEW_RESULT"):
                if event.result == RESULT_FAIL:
                    stage = "客户拒绝"
                elif event.result == RESULT_CANDIDATE_EXIT:
                    stage = "候选人拒绝"
                else:
                    stage = "初试"
            elif event.event_type == "OFFER":
                stage = "入职" if event.result == OFFER_ONBOARDED else "Offer"
            elif event.event_type == "ONBOARDED":
                stage = "入职"
            elif event.event_type == "EXIT":
                stage = "候选人拒绝" if event.result == RESULT_CANDIDATE_EXIT else "客户拒绝"
        if stage == "初试":
            current_round, _ = self._current_interview(session, case)
            if current_round is not None:
                stage = _interview_stage(current_round.round_no)
        case.stage = stage
        self._sync_candidate_and_reminders(session, case)

    def _sync_candidate_and_reminders(self, session: Session, case: CandidateJobCase) -> None:
        candidate = session.get(Candidate, case.candidate_id)
        if candidate is None:
            return
        session.flush()
        onboarded = self._has_active_onboarding(session, candidate.id)
        changed = False
        if onboarded and candidate.status not in ("ARCHIVED", "ON_HOLD"):
            candidate.workflow_previous_status = candidate.status
            candidate.status = "ON_HOLD"
            changed = True
        elif not onboarded and candidate.workflow_previous_status is not None:
            if candidate.status == "ON_HOLD":
                candidate.status = candidate.workflow_previous_status
                changed = True
            candidate.workflow_previous_status = None
        if changed:
            enqueue_sync(session, "candidate", candidate.id)
        # A hire blocks every open job for this person, but an exit belongs only
        # to its own case. Recompute all linked unfinished reminders on reversal.
        rows = session.execute(select(Reminder, CandidateJobCase, Jd)
            .join(CandidateJobCase, Reminder.case_id == CandidateJobCase.id)
            .outerjoin(Jd, CandidateJobCase.jd_id == Jd.id)
            .where(CandidateJobCase.candidate_id == candidate.id, Reminder.dismissed.is_(False)))
        for reminder, linked_case, jd in rows:
            terminal = linked_case.stage in ("入职", "客户拒绝", "候选人拒绝", "岗位关闭")
            reminder.paused_by_workflow = (terminal or onboarded or candidate.status != "AVAILABLE"
                or linked_case.deleted_at is not None or candidate.deleted_at is not None
                or jd is None or jd.deleted_at is not None or jd.status != "OPEN")
