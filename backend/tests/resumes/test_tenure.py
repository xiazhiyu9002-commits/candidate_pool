from __future__ import annotations

from datetime import date
from decimal import Decimal

from kerui_recruit.resumes.structured import NormalizedExperience
from kerui_recruit.resumes.tenure import compute_total_years, parse_month


def test_parse_month_accepts_common_resume_date_formats():
    expected = 2020 * 12 + 7 - 1
    for text in ("2020.07", "2020-7", "2020/07", "2020年7月"):
        assert parse_month(text) == expected
    # 只有年份时按该年 1 月计。
    assert parse_month("2020") == 2020 * 12 + 1 - 1
    assert parse_month("") is None
    assert parse_month("至今") is None


def test_compute_total_years_is_continuous_from_the_earliest_start():
    experiences = [
        {"start_date": "2022.03", "end_date": "至今"},
        {"start_date": "2020.07", "end_date": "2022.02"},
    ]
    # 最早 2020.07 → 2026.09 = 74 个月 ≈ 6.1667 年，量化到 0.1。
    assert compute_total_years(experiences, date(2026, 9, 20)) == Decimal("6.2")


def test_compute_total_years_includes_gaps_by_design():
    """连续口径会把中间空窗期算进去，这是既定口径，不是 bug。"""
    experiences = [
        {"start_date": "2010.01", "end_date": "2012.01"},
        {"start_date": "2024.01", "end_date": "至今"},  # 中间空窗 12 年
    ]
    years = compute_total_years(experiences, date(2026, 1, 15))
    assert years == Decimal("16.0")  # 而非各段相加的 4 年


def test_compute_total_years_grows_with_the_clock():
    experiences = [{"start_date": "2015.01", "end_date": "至今"}]
    assert compute_total_years(experiences, date(2025, 1, 5)) == Decimal("10.0")
    assert compute_total_years(experiences, date(2026, 1, 5)) == Decimal("11.0")


def test_compute_total_years_ignores_unparsable_entries():
    experiences = [
        {"start_date": "", "end_date": "至今"},
        {"title": "无起始时间的经历"},
        {"start_date": "2021.06", "end_date": "至今"},
    ]
    assert compute_total_years(experiences, date(2026, 6, 1)) == Decimal("5.0")


def test_compute_total_years_returns_none_when_undeterminable():
    assert compute_total_years([], date(2026, 9, 20)) is None
    assert compute_total_years(None, date(2026, 9, 20)) is None
    assert compute_total_years([{"title": "无日期"}], date(2026, 9, 20)) is None


def test_compute_total_years_rejects_future_start_dates():
    assert compute_total_years([{"start_date": "2030.01"}], date(2026, 9, 20)) is None


def test_compute_total_years_accepts_normalized_experience_objects():
    experience = NormalizedExperience(
        company="某公司", title="工程师", start_date="2018.05", end_date="至今", summary="负责业务系统"
    )
    assert compute_total_years([experience], date(2023, 5, 10)) == Decimal("5.0")


def test_compute_total_years_is_capped_at_the_schema_limit():
    assert compute_total_years([{"start_date": "1900.01"}], date(2026, 9, 20)) == Decimal("80.0")
