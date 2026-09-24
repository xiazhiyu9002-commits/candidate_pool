"""年滚动刷新：工作年限、年龄、以及画像文本里这两个数字。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import (
    Blob,
    Candidate,
    IndexSyncRecord,
    ResumeDocument,
    ResumeRevision,
)
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.resumes.work_years_rollover import (
    WorkYearsRollover,
    established_age_baseline,
)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    engine.dispose()


def _add_revision(
    session,
    document: ResumeDocument,
    *,
    start_date: str,
    total_years: str,
    age: int | None = None,
    profile: str | None = None,
) -> ResumeRevision:
    parsed: dict = {
        "name": "Test",
        "experiences": [{"company": "某公司", "title": "工程师", "start_date": start_date}],
        "total_years": total_years,
    }
    if age is not None:
        parsed["age"] = age
    if profile is not None:
        parsed["ai_profile_summary"] = profile
    sha = uuid4().hex
    revision = ResumeRevision(
        document=document,
        blob=Blob(content_sha256=sha, suffix=".pdf", size_bytes=1, storage_path=f"x-{sha}"),
        content_sha256=sha,
        original_filename="x.pdf",
        status="READY",
        is_current=True,
        parsed_data=parsed,
    )
    session.add(revision)
    session.flush()
    return revision


def _candidate(
    factory,
    *,
    start_date: str,
    total_years: str,
    age: int | None = None,
    profile: str | None = None,
    status: str = "AVAILABLE",
    is_current: bool = True,
) -> tuple[str, str]:
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status=status)
        document = ResumeDocument(candidate=candidate)
        session.add(document)
        session.flush()
        revision = _add_revision(
            session, document, start_date=start_date, total_years=total_years,
            age=age, profile=profile,
        )
        revision.is_current = is_current
        session.flush()
        return candidate.id, revision.id


def test_refresh_syncs_all_three_copies_and_follows_the_clock(factory) -> None:
    candidate_id, revision_id = _candidate(factory, start_date="2015.01", total_years="10.0")
    rollover = WorkYearsRollover(session_factory=factory)

    result = rollover.refresh(date(2026, 1, 5))

    assert result["updated"] == 1
    assert result["candidates"] == 1
    with factory() as session:
        revision = session.get(ResumeRevision, revision_id)
        candidate = session.get(Candidate, candidate_id)
        assert revision.parsed_data["total_years"] == "11.0"
        assert candidate.total_years == Decimal("11.0")
        # 索引列必须重建，否则会出现「显示 15 年、按 15 年筛不出来」。
        assert session.scalar(
            select(IndexSyncRecord).where(
                IndexSyncRecord.entity_type == "candidate",
                IndexSyncRecord.entity_id == candidate_id,
            )
        ) is not None


def test_refresh_is_idempotent_on_the_same_day(factory) -> None:
    _candidate(factory, start_date="2015.01", total_years="10.0")
    rollover = WorkYearsRollover(session_factory=factory)

    assert rollover.refresh(date(2026, 1, 5))["updated"] == 1
    assert rollover.refresh(date(2026, 1, 5))["updated"] == 0


def test_refresh_skips_unparsable_start_dates(factory) -> None:
    _candidate(factory, start_date="", total_years="10.0")
    rollover = WorkYearsRollover(session_factory=factory)
    assert rollover.refresh(date(2026, 1, 5))["updated"] == 0


def test_refresh_scope_excludes_archived_and_non_current_revisions(factory) -> None:
    """范围与画像回填/索引一致：归档、待复核、非当前版本都不参与滚动。"""
    _candidate(factory, start_date="2015.01", total_years="10.0", status="ARCHIVED")
    _candidate(factory, start_date="2015.01", total_years="10.0", status="PENDING_REVIEW")
    _candidate(factory, start_date="2015.01", total_years="10.0", is_current=False)
    rollover = WorkYearsRollover(session_factory=factory)

    result = rollover.refresh(date(2026, 1, 5))

    assert result["scanned"] == 0
    assert result["updated"] == 0


def test_refresh_batches_commits_without_losing_candidates(factory) -> None:
    """首日校正可能命中数百人：分批提交必须覆盖全部候选人，不能只处理最后一批。"""
    for _ in range(5):
        _candidate(factory, start_date="2015.01", total_years="10.0")
    rollover = WorkYearsRollover(session_factory=factory, commit_batch_size=2)

    result = rollover.refresh(date(2026, 1, 5))

    assert result["scanned"] == 5
    assert result["updated"] == 5
    with factory() as session:
        assert [c.total_years for c in session.scalars(select(Candidate)).all()] == [Decimal("11.0")] * 5


def test_batching_keeps_the_newest_revision_winning(factory) -> None:
    """分批后仍必须「更新的版本赢」：每批只处理 1 条时也要成立。"""
    candidate_id, _ = _candidate(factory, start_date="2011.01", total_years="11.0")
    with factory() as session, session.begin():
        document = session.scalar(
            select(ResumeDocument).where(ResumeDocument.candidate_id == candidate_id)
        )
        _add_revision(session, document, start_date="2013.01", total_years="12.0")
    rollover = WorkYearsRollover(session_factory=factory, commit_batch_size=1)

    rollover.refresh(date(2026, 1, 5))

    with factory() as session:
        assert session.get(Candidate, candidate_id).total_years == Decimal("13.0")


# --- 年龄 -------------------------------------------------------------------


def test_age_baseline_is_established_without_changing_the_age(factory) -> None:
    """不补历史欠账：首次刷新只建立基准，年龄当次不变，从本年起每年 +1。"""
    _, revision_id = _candidate(factory, start_date="2015.01", total_years="10.0", age=27)
    rollover = WorkYearsRollover(session_factory=factory)

    assert rollover.refresh(date(2026, 1, 5))["updated"] == 1

    with factory() as session:
        parsed = session.get(ResumeRevision, revision_id).parsed_data
        assert parsed["age"] == 27
        assert parsed["age_baseline"] == 27
        assert parsed["age_baseline_year"] == 2026


def test_age_grows_by_one_each_year(factory) -> None:
    _, revision_id = _candidate(factory, start_date="2015.01", total_years="10.0", age=27)
    rollover = WorkYearsRollover(session_factory=factory)
    rollover.refresh(date(2026, 1, 5))

    rollover.refresh(date(2026, 12, 31))
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["age"] == 27

    rollover.refresh(date(2027, 1, 1))
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["age"] == 28

    rollover.refresh(date(2030, 6, 1))
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["age"] == 31


def test_reset_baseline_keeps_growing_from_the_manual_value(factory) -> None:
    """人工改过年龄后仍要逐年增长：基准重置为「人工值 + 当年」。"""
    _, revision_id = _candidate(factory, start_date="2015.01", total_years="10.0", age=27)
    rollover = WorkYearsRollover(session_factory=factory)
    rollover.refresh(date(2026, 1, 5))

    with factory() as session, session.begin():
        revision = session.get(ResumeRevision, revision_id)
        parsed = dict(revision.parsed_data)
        parsed["age"] = 25  # 使用者手工改成 25
        parsed.update(established_age_baseline(25, 2026))
        revision.parsed_data = parsed

    rollover.refresh(date(2027, 3, 1))
    with factory() as session:
        assert session.get(ResumeRevision, revision_id).parsed_data["age"] == 26


def test_candidates_without_age_do_not_get_a_baseline(factory) -> None:
    _, revision_id = _candidate(factory, start_date="2015.01", total_years="10.0")
    rollover = WorkYearsRollover(session_factory=factory)

    rollover.refresh(date(2026, 1, 5))

    with factory() as session:
        parsed = session.get(ResumeRevision, revision_id).parsed_data
        assert "age_baseline" not in parsed
        assert "age" not in parsed


# --- 画像文本 ---------------------------------------------------------------


def test_profile_numbers_are_refreshed_in_place(factory) -> None:
    """画像只改年限与年龄两个数字，措辞一个字不动，也不调用模型。"""
    _, revision_id = _candidate(
        factory,
        start_date="2015.01",
        total_years="10.0",
        age=27,
        profile="该候选人，男，27岁，具备 10年后端开发经验。近3年专注交易系统，2020年入职现公司。",
    )
    rollover = WorkYearsRollover(session_factory=factory)

    rollover.refresh(date(2026, 1, 5))

    with factory() as session:
        parsed = session.get(ResumeRevision, revision_id).parsed_data
        assert parsed["ai_profile_summary"] == (
            "该候选人，男，27岁，具备 11年后端开发经验。近3年专注交易系统，2020年入职现公司。"
        )


def test_profile_age_is_refreshed_with_the_years(factory) -> None:
    """画像里的年龄以字段年龄为准（字段是权威值），并随年份一起增长。"""
    _, revision_id = _candidate(
        factory,
        start_date="2015.01",
        total_years="10.0",
        age=27,
        profile="张伟，27岁，10年后端开发经验。",
    )
    rollover = WorkYearsRollover(session_factory=factory)
    rollover.refresh(date(2026, 1, 5))  # 年限 10.0 → 11.0；年龄只建立基准，当次不变

    rollover.refresh(date(2027, 2, 1))  # 年龄 27 → 28，年限 11.0 → 12.2

    with factory() as session:
        parsed = session.get(ResumeRevision, revision_id).parsed_data
        assert parsed["age"] == 28
        assert parsed["ai_profile_summary"] == "张伟，28岁，12年后端开发经验。"


def test_profile_without_replaceable_numbers_is_left_untouched(factory) -> None:
    """画像里没有可安全替换的总年限时保持原样，不猜、不改。"""
    original = "李四，专注风控建模，在多家券商任职，2024年转入私募。"
    _, revision_id = _candidate(
        factory, start_date="2015.01", total_years="10.0", profile=original
    )
    rollover = WorkYearsRollover(session_factory=factory)

    rollover.refresh(date(2026, 1, 5))

    with factory() as session:
        parsed = session.get(ResumeRevision, revision_id).parsed_data
        assert parsed["ai_profile_summary"] == original