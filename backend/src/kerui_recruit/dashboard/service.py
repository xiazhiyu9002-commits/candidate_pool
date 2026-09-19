from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.cases.projection import effective_events
from kerui_recruit.cases.service import (
    OFFER_ACCEPTED, OFFER_ISSUED, OFFER_ONBOARDED, RESULT_CANCELLED,
    RESULT_CANDIDATE_EXIT, RESULT_FAIL, RESULT_PASS, RESULT_PENDING,
    RESULT_SKIPPED, SHANGHAI,
)
from kerui_recruit.db.models import Candidate, CandidateJobCase, CaseEvent, CaseRound, Jd


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    company: str | None = None
    jd_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    def __post_init__(self):
        if self.date_from and self.date_to and _to_utc(self.date_from) >= _to_utc_end(self.date_to):
            raise ValueError("开始日期不能晚于结束日期")


def _period_key(dt: datetime, granularity: str) -> str:
    local = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(SHANGHAI)
    if granularity == "week":
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    if granularity == "quarter":
        return f"{local.year}-Q{(local.month - 1) // 3 + 1}"
    return f"{local.year}-{local.month:02d}"


def _bucket_sequence(start: datetime, end: datetime, granularity: str) -> list[str]:
    start = (start.replace(tzinfo=SHANGHAI) if start.tzinfo is None else start).astimezone(SHANGHAI)
    end = (end.replace(tzinfo=SHANGHAI) if end.tzinfo is None else end).astimezone(SHANGHAI)
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        cursor -= timedelta(days=cursor.weekday())
    elif granularity == "quarter":
        cursor = cursor.replace(month=((cursor.month - 1) // 3) * 3 + 1, day=1)
    else:
        cursor = cursor.replace(day=1)
    result = []
    while cursor <= end:
        result.append(_period_key(cursor, granularity))
        if granularity == "week":
            cursor += timedelta(days=7)
        else:
            month = cursor.month + (3 if granularity == "quarter" else 1)
            cursor = cursor.replace(year=cursor.year + (month - 1) // 12, month=(month - 1) % 12 + 1)
    return result


def _to_utc(dt: datetime) -> datetime:
    return (dt.replace(tzinfo=SHANGHAI) if dt.tzinfo is None else dt).astimezone(timezone.utc).replace(tzinfo=None)


def _to_utc_end(dt: datetime) -> datetime:
    local = (dt.replace(tzinfo=SHANGHAI) if dt.tzinfo is None else dt).astimezone(SHANGHAI)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)


class DashboardService:
    """One effective-event projection shared by cards, trends, tables and export."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _in_period(value: datetime, filters: DashboardFilters) -> bool:
        return ((filters.date_from is None or value >= _to_utc(filters.date_from))
                and (filters.date_to is None or value < _to_utc_end(filters.date_to)))

    def _snapshot(self, session: Session, filters: DashboardFilters):
        scope = (select(CandidateJobCase, Jd)
                 .join(Jd, CandidateJobCase.jd_id == Jd.id)
                 .join(Candidate, CandidateJobCase.candidate_id == Candidate.id)
                 .where(CandidateJobCase.deleted_at.is_(None), Jd.deleted_at.is_(None), Candidate.deleted_at.is_(None)))
        if filters.jd_id:
            scope = scope.where(Jd.id == filters.jd_id)
        if filters.company:
            scope = scope.where(Jd.company == filters.company)
        cases = {case.id: (case, jd) for case, jd in session.execute(scope)}
        if not cases:
            return cases, [], {}, {}, {}
        # First facts are resolved over history before applying the report start.
        # 无结束日期时不设截止时间，未来录入的业务事实也纳入统计，保证与流程面板一致。
        scoped_ids = scope.with_only_columns(CandidateJobCase.id)
        cutoff = _to_utc_end(filters.date_to) if filters.date_to else None
        event_query = select(CaseEvent).where(CaseEvent.case_id.in_(scoped_ids))
        if cutoff is not None:
            event_query = event_query.where(CaseEvent.occurred_at < cutoff)
        events = effective_events(list(session.scalars(event_query)), cutoff)
        rounds = {r.id: r for r in session.scalars(select(CaseRound).where(CaseRound.case_id.in_(scoped_ids)))}
        rec, off = {}, {}
        for event in events:
            if event.event_type == "RECOMMENDED":
                rec.setdefault(event.case_id, event.occurred_at)
            elif event.event_type == "OFFER" and event.result == OFFER_ISSUED:
                off.setdefault(event.case_id, event.occurred_at)
        return cases, events, rounds, rec, off

    def overview(self, filters: DashboardFilters | None = None) -> dict:
        filters = filters or DashboardFilters()
        with self.session_factory() as session:
            _, events, _, rec, off = self._snapshot(session, filters)
            offered = {cid for cid, occurred in off.items() if self._in_period(occurred, filters)}
            latest = {e.case_id: e for e in events if e.event_type in ("OFFER", "EXIT", "ONBOARDED")}
            onboarded = set()
            active = 0
            for cid in offered:
                last = latest.get(cid)
                if last is None:
                    continue
                if last.event_type == "ONBOARDED" or last.result == OFFER_ONBOARDED:
                    onboarded.add(cid)
                elif last.event_type != "EXIT" and last.result in (OFFER_ISSUED, OFFER_ACCEPTED):
                    active += 1
            created = list(session.scalars(select(Candidate.created_at).where(Candidate.deleted_at.is_(None))))
        monthly = defaultdict(int)
        for occurred in created:
            monthly[_period_key(occurred, "month")] += 1
        return {"recommendation_total": sum(self._in_period(dt, filters) for dt in rec.values()),
                "offer_total": len(offered), "active_offer_total": active,
                "onboarded_total": len(onboarded),
                "candidate_total": len(created), "candidate_scope": "all_active",
                "monthly_new_candidates": [{"month": month, "count": count} for month, count in sorted(monthly.items())]}

    def trend(self, granularity: str, filters: DashboardFilters | None = None) -> list[dict]:
        if granularity not in ("week", "month", "quarter"):
            raise ValueError(f"Unknown granularity: {granularity}")
        filters = filters or DashboardFilters()
        with self.session_factory() as session:
            _, _, _, rec, off = self._snapshot(session, filters)
        buckets, dates = {}, []
        for metric, values in (("recommendation", rec), ("offer", off)):
            for occurred in values.values():
                if self._in_period(occurred, filters):
                    dates.append(occurred.replace(tzinfo=timezone.utc))
                    key = _period_key(occurred, granularity)
                    buckets.setdefault(key, {"period": key, "recommendation": 0, "offer": 0})[metric] += 1
        start = filters.date_from or (min(dates) if dates else None)
        end = filters.date_to or (max(dates) if dates else None)
        if start is not None and end is not None:
            for key in _bucket_sequence(start, end, granularity):
                buckets.setdefault(key, {"period": key, "recommendation": 0, "offer": 0})
        return [buckets[key] for key in sorted(buckets)]

    def by_jd(self, filters: DashboardFilters | None = None) -> list[dict]:
        filters = filters or DashboardFilters()
        with self.session_factory() as session:
            cases, events, rounds, rec, off = self._snapshot(session, filters)
        recommended = {cid for cid, dt in rec.items() if self._in_period(dt, filters)}
        offered = {cid for cid, dt in off.items() if self._in_period(dt, filters)}
        onboarded = {}
        for event in events:
            if event.event_type == "ONBOARDED" or (event.event_type == "OFFER" and event.result == OFFER_ONBOARDED):
                onboarded.setdefault(event.case_id, event.occurred_at)
        onboarded_in_period = {cid for cid, dt in onboarded.items() if self._in_period(dt, filters)}
        entered, latest_result = {}, {}
        for event in events:
            if event.event_type == "INTERVIEW_ENTERED":
                entered.setdefault(event.case_round_id, event)
            elif event.event_type == "INTERVIEW_RESULT":
                latest_result[event.case_round_id] = event
        metrics = {}

        def job(case_id):
            case, jd = cases[case_id]
            return metrics.setdefault(jd.id, {"jd_id": jd.id, "company": jd.company, "title": jd.title,
                "recommendation_total": 0, "offer_total": 0, "converted_total": 0,
                "unattributed_offer_total": 0, "onboarded_total": 0, "final_offer_rate": None, "rounds": {}})

        for cid in recommended:
            item = job(cid)
            item["recommendation_total"] += 1
            item["converted_total"] += int(cid in off and off[cid] >= rec[cid])
        for cid in offered:
            item = job(cid)
            item["offer_total"] += 1
            item["unattributed_offer_total"] += int(cid not in rec or off[cid] < rec[cid])
        for cid in onboarded_in_period:
            job(cid)["onboarded_total"] += 1
        names = {RESULT_PASS: "passed", RESULT_FAIL: "failed", RESULT_PENDING: "pending",
                 RESULT_SKIPPED: "skipped", RESULT_CANDIDATE_EXIT: "exited", RESULT_CANCELLED: "cancelled"}
        for rid, event in entered.items():
            round_ = rounds.get(rid)
            if round_ is None:
                continue
            result = latest_result.get(rid)
            in_entered = self._in_period(event.occurred_at, filters)
            in_result = result is not None and self._in_period(result.occurred_at, filters)
            if not in_entered and not in_result:
                continue
            key = getattr(round_, "definition_key", None) or f"{round_.round_no}:{round_.round_name}:{round_.round_type or ''}"
            bucket = job(event.case_id)["rounds"].setdefault(key, {"round_key": key,
                "round_no": round_.round_no, "round_name": round_.round_name, "round_type": round_.round_type,
                **{name: set() for name in ("entered", *names.values())}})
            if in_entered:
                bucket["entered"].add(event.case_id)
            if in_result and result.result in names:
                bucket[names[result.result]].add(event.case_id)
            elif in_entered and result is None:
                bucket["pending"].add(event.case_id)
        for item in metrics.values():
            rec_count = item["recommendation_total"]
            item["final_offer_rate"] = round(item["converted_total"] / rec_count, 4) if rec_count else None
            round_metrics = []
            for bucket in item["rounds"].values():
                value = {k: len(v) if isinstance(v, set) else v for k, v in bucket.items()}
                value["judged"] = value["passed"] + value["failed"]
                value["pass_rate"] = round(value["passed"] / value["judged"], 4) if value["judged"] else None
                round_metrics.append(value)
            item["rounds"] = sorted(round_metrics, key=lambda r: (r["round_no"], r["round_key"]))
        return sorted(metrics.values(), key=lambda item: (item["company"], item["title"]))

    def export(self, filters: DashboardFilters | None = None) -> bytes:
        import io
        from openpyxl import Workbook
        jobs = self.by_jd(filters)
        workbook = Workbook()
        summary = workbook.active
        summary.title = "汇总"
        headers = ["公司", "岗位", "推荐量", "offer量", "推荐队列转化率", "入职人数"]
        summary.append(headers)
        for index, jd in enumerate(jobs):
            row = [jd["company"], jd["title"], jd["recommendation_total"], jd["offer_total"], jd["final_offer_rate"], jd["onboarded_total"]]
            summary.append(row)
            sheet = workbook.create_sheet(title=f"岗位{index + 1}")
            sheet.append(headers)
            sheet.append(row)
            sheet.append([])
            sheet.append(["轮次", "轮次名", "进入", "通过", "未通过", "待反馈", "跳过", "退出", "取消", "通过率"])
            for r in jd["rounds"]:
                sheet.append([r["round_no"], r["round_name"], r["entered"], r["passed"], r["failed"], r["pending"], r["skipped"], r["exited"], r["cancelled"], r["pass_rate"]])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
