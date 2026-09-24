"""年限硬窗口 [n-1, 2n] 与业务方向按比例置顶的单测（方案 §3 / §4）。"""
from __future__ import annotations

from kerui_recruit.match.service import (
    _hard_filter,
    _JdContext,
    _meets_years,
    _years_window,
    merge_by_business,
)
from kerui_recruit.search.contracts import CandidateFilters, SearchHit


def _context(min_years: float | None) -> _JdContext:
    return _JdContext(revision_id="rev", jd_id="jd", source_text=None, min_years=min_years,
                      highest_degree=None, location=None, parsed_data={})


def _hit(years: float | None) -> SearchHit:
    return SearchHit(chunk_id="c1", candidate_id="cand-1", revision_id="rev-1", content="x",
                     score=0.0, matched_channels=(), total_years=years,
                     highest_degree=None, location=None)


# ---------- §3 年限窗口 ----------


def test_years_window_edges() -> None:
    # n 缺失 → 不设窗口（不得套 0~3：本池 0~3 年只有十几人）。
    assert _years_window(None) is None
    # 一年经验（含 0.5 这类不足一年）→ 1~3。
    assert _years_window(1) == (1.0, 3.0)
    assert _years_window(0.5) == (1.0, 3.0)
    # n>=2 → n-1 ~ 2n。
    assert _years_window(3) == (2.0, 6.0)
    assert _years_window(5) == (4.0, 10.0)
    assert _years_window(6) == (5.0, 12.0)
    assert _years_window(10) == (9.0, 20.0)


def test_hard_filter_sets_both_window_bounds() -> None:
    filters = _hard_filter(_context(5), None)
    assert (filters.min_years, filters.max_years) == (4.0, 10.0)


def test_hard_filter_without_min_years_sets_no_window() -> None:
    filters = _hard_filter(_context(None), None)
    assert filters.min_years is None
    assert filters.max_years is None


def test_hard_filter_window_overrides_provided_bounds() -> None:
    """JD 的年限窗口覆盖调用方传入的年限条件，避免两套口径并存。"""
    filters = _hard_filter(_context(3), CandidateFilters(min_years=8.0, max_years=20.0))
    assert (filters.min_years, filters.max_years) == (2.0, 6.0)


def test_meets_years_requires_inside_window() -> None:
    context = _context(3)  # 窗口 2~6
    assert _meets_years(context, _hit(2.0)) is True
    assert _meets_years(context, _hit(6.0)) is True
    assert _meets_years(context, _hit(1.9)) is False
    assert _meets_years(context, _hit(6.1)) is False


def test_meets_years_without_window_is_always_true() -> None:
    assert _meets_years(_context(None), _hit(0.0)) is True
    assert _meets_years(_context(None), _hit(30.0)) is True


# ---------- §4 业务方向按比例置顶 ----------


def _scored(preferred: int, others: int) -> list[tuple[str, bool | None]]:
    return [(f"p{i}", True) for i in range(preferred)] + [(f"o{i}", False) for i in range(others)]


def test_quota_keeps_twenty_percent_for_others() -> None:
    result = merge_by_business(_scored(60, 100), 50)
    assert len(result) == 50
    assert result[:40] == [f"p{i}" for i in range(40)]
    assert result[40:] == [f"o{i}" for i in range(10)]


def test_short_group_gives_slots_back_to_the_other() -> None:
    """其余组不足 10 个时，空出的名额回补给置顶组，保证返满。"""
    result = merge_by_business(_scored(60, 3), 50)
    assert len(result) == 50
    assert sum(1 for item in result if item.startswith("p")) == 47
    assert result[-3:] == ["o0", "o1", "o2"]


def test_preferred_group_short_falls_back_to_others() -> None:
    result = merge_by_business(_scored(5, 100), 50)
    assert len(result) == 50
    assert result[:5] == [f"p{i}" for i in range(5)]


def test_flag_none_joins_the_others_group() -> None:
    """任一侧缺业务方向（None）不惩罚也不置顶。"""
    assert merge_by_business([("a", None), ("b", True)], 2) == ["b", "a"]


def test_small_limit_keeps_ratio() -> None:
    result = merge_by_business(_scored(20, 20), 10)
    assert len(result) == 10
    assert sum(1 for item in result if item.startswith("p")) == 8


def test_empty_input_and_zero_limit() -> None:
    assert merge_by_business([], 10) == []
    assert merge_by_business(_scored(3, 3), 0) == []
