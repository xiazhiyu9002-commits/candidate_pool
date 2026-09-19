from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.cases.state import refresh_links
from kerui_recruit.db.models import (
    Candidate,
    CandidateContact,
    CandidateJobCase,
    MatchResult,
    Reminder,
    ResumeDocument,
    ResumeRevision,
)
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.search.sync import enqueue_sync


_TERMINAL_STAGES = frozenset({"入职", "客户拒绝", "候选人拒绝", "岗位关闭"})
_ACTIVE_CASE_STAGES = frozenset({"待评估", "待联系", "已联系", "有意向", "已推荐", "初试", "复试", "终试", "Offer"})


def normalize_phone(phone: str | None) -> str | None:
    """Normalize a phone to a comparable fingerprint (digits only, CN 86 prefix dropped)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits or None


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    cleaned = email.strip().casefold()
    return cleaned or None


class DuplicateReportService:
    """Read-only duplicate candidate report; never mutates any record."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        encryption: EncryptionService,
        exports_dir: Path,
    ) -> None:
        self.session_factory = session_factory
        self.encryption = encryption
        self.exports_dir = exports_dir

    def generate(self) -> dict:
        with self.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(Candidate).where(Candidate.deleted_at.is_(None)).order_by(Candidate.created_at.asc())
                ).all()
            )
            contacts = {
                row.candidate_id: row
                for row in session.execute(
                    select(CandidateContact).where(CandidateContact.candidate_id.in_([c.id for c in candidates]))
                ).scalars()
            } if candidates else {}

            revisions = list(
                session.execute(
                    select(ResumeRevision, ResumeDocument.candidate_id)
                    .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                    .where(ResumeDocument.candidate_id.in_([c.id for c in candidates]))
                ).all()
            ) if candidates else []

            case_counts = dict(
                session.execute(
                    select(CandidateJobCase.candidate_id, func.count())
                    .where(CandidateJobCase.candidate_id.in_([c.id for c in candidates]))
                    .group_by(CandidateJobCase.candidate_id)
                ).all()
            ) if candidates else {}

            active_case_counts = dict(
                session.execute(
                    select(CandidateJobCase.candidate_id, func.count())
                    .where(
                        CandidateJobCase.candidate_id.in_([c.id for c in candidates]),
                        CandidateJobCase.deleted_at.is_(None),
                        CandidateJobCase.stage.in_(_ACTIVE_CASE_STAGES),
                    )
                    .group_by(CandidateJobCase.candidate_id)
                ).all()
            ) if candidates else {}

            match_counts = dict(
                session.execute(
                    select(MatchResult.candidate_id, func.count())
                    .where(MatchResult.candidate_id.in_([c.id for c in candidates]))
                    .group_by(MatchResult.candidate_id)
                ).all()
            ) if candidates else {}

            reminder_counts = dict(
                session.execute(
                    select(CandidateJobCase.candidate_id, func.count())
                    .join(Reminder, Reminder.case_id == CandidateJobCase.id)
                    .where(CandidateJobCase.candidate_id.in_([c.id for c in candidates]))
                    .group_by(CandidateJobCase.candidate_id)
                ).all()
            ) if candidates else {}

        revs_by_candidate: dict[str, list[ResumeRevision]] = {}
        for revision, candidate_id in revisions:
            revs_by_candidate.setdefault(candidate_id, []).append(revision)

        by_fingerprint: dict[tuple[str, str], list[str]] = {}
        for candidate in candidates:
            contact = contacts.get(candidate.id)
            phone = None
            email = None
            if contact:
                phone = normalize_phone(self.encryption.decrypt(contact.phone_encrypted) if contact.phone_encrypted else None)
                email = normalize_email(self.encryption.decrypt(contact.email_encrypted) if contact.email_encrypted else None)
            by_fingerprint.setdefault((phone or "", email or ""), []).append(candidate.id)

        groups = []
        cand_by_id = {c.id: c for c in candidates}
        for (phone, email), ids in by_fingerprint.items():
            if len(ids) < 2:
                continue
            rows = [
                self._row(
                    cid,
                    cand_by_id[cid].display_name,
                    contacts.get(cid),
                    revs_by_candidate.get(cid, []),
                    case_counts.get(cid, 0),
                    active_case_counts.get(cid, 0),
                    match_counts.get(cid, 0),
                    reminder_counts.get(cid, 0),
                )
                for cid in ids
            ]
            primary = self._recommend_primary(rows)
            groups.append({"group_id": f"{phone or 'email'}:{email or phone}", "rows": rows, "primary_candidate_id": primary})

        summary = self._summarize(groups)
        csv_path = self._write_csv(groups)
        return {"groups": groups, "summary": summary, "csv_path": str(csv_path)}

    def _row(self, candidate_id, display_name, contact, revisions, case_count, active_case_count, match_count, reminder_count):
        current = next((r for r in revisions if r.is_current), None)
        has_manual = bool(contact and contact.manual_fields)
        has_phone = bool(contact and contact.phone_encrypted)
        has_email = bool(contact and contact.email_encrypted)
        return {
            "candidate_id": candidate_id,
            "display_name": display_name,
            "content_sha256": (current.content_sha256 if current else None),
            "revision_count": len(revisions),
            "current_status": (current.status if current else None),
            "current_original_filename": (current.original_filename if current else None),
            "has_phone": has_phone,
            "has_email": has_email,
            "case_count": case_count,
            "active_case_count": active_case_count,
            "match_count": match_count,
            "reminder_count": reminder_count,
            "has_manual_fields": has_manual,
            "parsed_ready": bool(current and current.status == "READY" and current.parsed_data),
        }

    def _recommend_primary(self, rows):
        # Prefer active cases, manual fields, complete contact, READY revision, then stable id.
        def key(row):
            return (
                row["active_case_count"] > 0,
                row["has_manual_fields"],
                row["has_phone"] and row["has_email"],
                row["parsed_ready"],
                row["candidate_id"],
            )
        return max(rows, key=key)["candidate_id"]

    def _summarize(self, groups):
        extra = sum(len(g["rows"]) - 1 for g in groups)
        return {
            "group_count": len(groups),
            "candidate_count": sum(len(g["rows"]) for g in groups),
            "extra_candidate_count": extra,
            "groups_with_cases": sum(1 for g in groups if any(r["case_count"] > 0 for r in g["rows"])),
            "groups_with_contact_conflict": sum(1 for g in groups if len({r["has_phone"] for r in g["rows"]}) > 1 or len({r["has_email"] for r in g["rows"]}) > 1),
            "groups_with_manual_conflict": sum(1 for g in groups if sum(1 for r in g["rows"] if r["has_manual_fields"]) > 1),
            "low_risk_mergeable": sum(1 for g in groups if all(not r["has_manual_fields"] for r in g["rows"]) and sum(1 for r in g["rows"] if r["active_case_count"] > 0) <= 1),
            "requires_manual_review": sum(1 for g in groups if any(r["has_manual_fields"] for r in g["rows"]) or sum(1 for r in g["rows"] if r["active_case_count"] > 0) > 1),
        }

    def _write_csv(self, groups):
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        path = self.exports_dir / "duplicate_candidates.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["duplicate_group_id", "candidate_id", "content_sha256", "revision_count", "current_status", "has_phone", "has_email", "case_count", "active_case_count", "match_count", "reminder_count", "has_manual_fields", "primary_candidate_id", "merge_status"])
            for group in groups:
                for row in group["rows"]:
                    writer.writerow([
                        group["group_id"], row["candidate_id"], row["content_sha256"], row["revision_count"],
                        row["current_status"], row["has_phone"], row["has_email"], row["case_count"],
                        row["active_case_count"], row["match_count"], row["reminder_count"],
                        row["has_manual_fields"], group["primary_candidate_id"], "PENDING_REVIEW",
                    ])
        return path


class MergePlanService:
    """Produce a merge dry-run plan without mutating anything."""

    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def plan(self, *, group_id: str, primary_candidate_id: str, duplicate_candidate_ids: list[str]) -> dict:
        with self.session_factory() as session:
            documents = list(
                session.scalars(
                    select(ResumeDocument).where(ResumeDocument.candidate_id.in_(duplicate_candidate_ids))
                ).all()
            )
            revision_ids = [d.id for d in documents]
            revisions = list(session.scalars(select(ResumeRevision).where(ResumeRevision.document_id.in_(revision_ids))).all()) if revision_ids else []
            cases = list(session.scalars(select(CandidateJobCase).where(CandidateJobCase.candidate_id.in_(duplicate_candidate_ids))).all())
            matches = list(session.scalars(select(MatchResult).where(MatchResult.candidate_id.in_(duplicate_candidate_ids))).all())
            reminders = list(session.scalars(select(Reminder).where(Reminder.case_id.in_([c.id for c in cases]))).all()) if cases else []

        return {
            "group_id": group_id,
            "primary_candidate_id": primary_candidate_id,
            "duplicate_candidate_ids": duplicate_candidate_ids,
            "dry_run": True,
            "planned_actions": {
                "move_documents": len(documents),
                "move_revisions": len(revisions),
                "move_cases": len(cases),
                "move_matches": len(matches),
                "move_reminders": len(reminders),
                "contact_policy": "主候选人联系方式优先，冲突字段写入待复核",
                "candidate_status_policy": "保留主候选人状态；重复候选人最终软删除",
                "reindex_required": True,
                "rollback_available": True,
                "constraint_conflicts": [],
            },
            "duplicates_soft_deleted": duplicate_candidate_ids,
        }

    def find_identical_groups(self) -> list[dict]:
        """按当前版本 content_sha256 分组，返回包含多个候选人的完全相同文件组。"""
        with self.session_factory() as session:
            rows = session.execute(
                select(ResumeDocument.candidate_id, ResumeRevision.content_sha256)
                .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
                .where(
                    ResumeRevision.is_current.is_(True),
                    Candidate.deleted_at.is_(None),
                )
            ).all()
        by_sha: dict[str, list[str]] = {}
        for candidate_id, sha in rows:
            by_sha.setdefault(sha, []).append(candidate_id)
        return [
            {"content_sha256": sha, "candidate_ids": sorted(set(ids))}
            for sha, ids in by_sha.items()
            if len(set(ids)) > 1
        ]

    def execute_merge(self, candidate_ids: list[str]) -> dict:
        """合并完全相同文件组：保留主候选人，其余软删并把流程/匹配改挂到主候选人。"""
        unique_ids = sorted(set(candidate_ids))
        if len(unique_ids) < 2:
            raise ValueError("至少需要两个候选人才能合并")

        with self.session_factory() as session, session.begin():
            candidates = list(
                session.scalars(
                    select(Candidate).where(Candidate.id.in_(unique_ids))
                ).all()
            )
            if len(candidates) != len(unique_ids):
                raise LookupError("部分候选人不存在")

            primary = self._pick_primary(session, candidates)
            duplicate_ids = [c.id for c in candidates if c.id != primary.id]

            moved_cases = 0
            moved_matches = 0
            for dup_id in duplicate_ids:
                moved_cases += session.execute(
                    update(CandidateJobCase)
                    .where(CandidateJobCase.candidate_id == dup_id)
                    .values(candidate_id=primary.id)
                ).rowcount
                moved_matches += session.execute(
                    update(MatchResult)
                    .where(MatchResult.candidate_id == dup_id)
                    .values(candidate_id=primary.id)
                ).rowcount

            for dup in candidates:
                if dup.id == primary.id:
                    continue
                dup.deleted_at = datetime.now(timezone.utc)
                enqueue_sync(session, "candidate", dup.id)
                refresh_links(session, candidate_id=dup.id)

            enqueue_sync(session, "candidate", primary.id)
            return {
                "primary_candidate_id": primary.id,
                "duplicate_candidate_ids": sorted(duplicate_ids),
                "moved_cases": moved_cases,
                "moved_matches": moved_matches,
            }

    @staticmethod
    def _pick_primary(session: Session, candidates: list[Candidate]) -> Candidate:
        active_counts = {
            cid: count
            for cid, count in session.execute(
                select(CandidateJobCase.candidate_id, func.count())
                .where(
                    CandidateJobCase.candidate_id.in_([c.id for c in candidates]),
                    CandidateJobCase.deleted_at.is_(None),
                    CandidateJobCase.stage.in_(_ACTIVE_CASE_STAGES),
                )
                .group_by(CandidateJobCase.candidate_id)
            ).all()
        }
        return max(
            candidates,
            key=lambda c: (active_counts.get(c.id, 0), c.id),
        )
