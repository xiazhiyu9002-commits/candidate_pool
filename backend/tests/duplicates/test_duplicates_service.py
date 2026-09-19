from pathlib import Path

from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CandidateContact
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.duplicates.service import (
    DuplicateReportService,
    MergePlanService,
    normalize_email,
    normalize_phone,
)
from kerui_recruit.encryption.service import EncryptionService


def _make(tmp_path: Path):
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    return factory, encryption


def _seed_candidate(factory, encryption, name, phone=None, email=None) -> str:
    with factory() as session:
        candidate = Candidate(display_name=name, status="AVAILABLE")
        session.add(candidate)
        session.flush()
        session.add(CandidateContact(
            candidate_id=candidate.id,
            phone_encrypted=encryption.encrypt(phone) if phone else None,
            email_encrypted=encryption.encrypt(email) if email else None,
        ))
        session.commit()
        return candidate.id


def test_normalize_phone_and_email() -> None:
    assert normalize_phone("138-0013-8000") == "13800138000"
    assert normalize_phone("8613800138000") == "13800138000"
    assert normalize_phone(None) is None
    assert normalize_email(" Foo@Bar.COM ") == "foo@bar.com"
    assert normalize_email(None) is None


def test_report_groups_duplicates_by_phone(tmp_path: Path) -> None:
    factory, encryption = _make(tmp_path)
    _seed_candidate(factory, encryption, "张三", phone="13800138000")
    _seed_candidate(factory, encryption, "张三-重复", phone="13800138000")
    _seed_candidate(factory, encryption, "李四", phone="13900139000")

    service = DuplicateReportService(
        session_factory=factory, encryption=encryption, exports_dir=tmp_path / "exports"
    )
    report = service.generate()

    assert report["summary"]["group_count"] == 1
    assert report["summary"]["candidate_count"] == 2
    assert report["summary"]["extra_candidate_count"] == 1
    assert (tmp_path / "exports" / "duplicate_candidates.csv").exists()


def test_report_no_duplicates(tmp_path: Path) -> None:
    factory, encryption = _make(tmp_path)
    _seed_candidate(factory, encryption, "张三", phone="13800138000")
    _seed_candidate(factory, encryption, "李四", phone="13900139000")

    service = DuplicateReportService(
        session_factory=factory, encryption=encryption, exports_dir=tmp_path / "exports"
    )
    report = service.generate()

    assert report["summary"]["group_count"] == 0
    assert report["summary"]["candidate_count"] == 0


def test_merge_plan_is_dry_run(tmp_path: Path) -> None:
    factory, encryption = _make(tmp_path)
    primary = _seed_candidate(factory, encryption, "主候选人", phone="13800138000")
    dup = _seed_candidate(factory, encryption, "重复候选人", phone="13800138000")

    service = MergePlanService(session_factory=factory)
    plan = service.plan(
        group_id="13800138000",
        primary_candidate_id=primary,
        duplicate_candidate_ids=[dup],
    )

    assert plan["dry_run"] is True
    assert plan["primary_candidate_id"] == primary
    assert plan["duplicates_soft_deleted"] == [dup]
    assert plan["planned_actions"]["reindex_required"] is True
