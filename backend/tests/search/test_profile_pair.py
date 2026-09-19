"""画像双形态字段与父/子切片向量文本测试。"""
from __future__ import annotations

from kerui_recruit.resumes.structured import ParsedResume
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.search.documents import build_child_documents


def test_parsed_resume_accepts_dual_profile_fields():
    parsed = ParsedResume(
        ai_profile_narrative="7年后端与数据平台研发经验的资深工程师",
        ai_profile_points=[
            {"text": "深耕数据平台与 Agent 应用", "evidence_paths": ["projects[0].summary"]},
            {"text": "主导高并发交易系统", "evidence_paths": []},
        ],
        ai_profile_compact="后端与数据平台资深工程师",
    )
    assert parsed.ai_profile_points[0].text == "深耕数据平台与 Agent 应用"
    assert parsed.ai_profile_points[0].evidence_paths == ["projects[0].summary"]
    assert parsed.ai_profile_compact == "后端与数据平台资深工程师"


def test_parsed_jd_accepts_dual_profile_fields():
    parsed = ParsedJd(
        title="后端",
        candidate_profile_narrative="需要资深后端工程师",
        candidate_profile_points=[
            {"text": "Java 核心主栈", "evidence_paths": ["required_skills[0]"]},
            {"text": "交易系统经验", "evidence_paths": []},
        ],
        candidate_profile_compact="资深后端工程师",
    )
    assert parsed.candidate_profile_points[0].text == "Java 核心主栈"


def test_build_child_documents_adds_profile_points_with_compact_prefix():
    children = build_child_documents({
        "ai_profile_summary": "7年后端研发\n主导交易系统",
        "ai_profile_compact": "后端研发",
        "ai_profile_points": [
            {"text": "深耕数据平台", "evidence_paths": ["projects[0].summary"]},
        ],
        "experiences": [{"company": "某司", "title": "后端", "summary": "负责交易系统"}],
        "projects": [{"summary": "搭建高并发交易系统"}],
    })
    kinds = [c["kind"] for c in children]
    assert "profile_point" in kinds
    assert "experience" in kinds
    assert "project" in kinds
    point = next(c for c in children if c["kind"] == "profile_point")
    assert point["vector_text"].startswith("后端研发 ")
    assert point["evidence_path"] == ["projects[0].summary"]
