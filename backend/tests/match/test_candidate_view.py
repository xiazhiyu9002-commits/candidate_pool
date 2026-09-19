"""多份当前简历的统一人选证据视图（candidate_view）。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from kerui_recruit.match.candidate_view import build_candidate_view


def _rev(rev_id: str, created_at: datetime, parsed_data: dict) -> SimpleNamespace:
    return SimpleNamespace(id=rev_id, created_at=created_at, parsed_data=parsed_data)


def test_view_uses_latest_hard_fields_and_merges_skills():
    old = _rev("old", datetime(2020, 1, 1), {"direction": "BACKEND", "skills": ["Java"], "total_years": 5})
    new = _rev("new", datetime(2021, 1, 1), {
        "direction": "DATA", "skills": ["SQL", "Python"], "total_years": 6,
        "projects": [{"summary": "数仓建模"}],
    })
    view = build_candidate_view([old, new], None)
    assert view["direction"] == "DATA"
    assert view["total_years"] == 6
    assert set(view["skills"]) >= {"Java", "SQL", "Python"}
    assert view["projects"]


def test_view_dedupes_skills_across_revisions():
    old = _rev("old", datetime(2020, 1, 1), {"skills": ["Java", "MySQL"]})
    new = _rev("new", datetime(2021, 1, 1), {"skills": ["Java", "Redis"]})
    view = build_candidate_view([old, new], None)
    assert view["skills"].count("Java") == 1
    assert set(view["skills"]) >= {"Java", "MySQL", "Redis"}


def test_view_empty_when_no_revisions():
    assert build_candidate_view([], None) == {}
