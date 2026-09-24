"""匹配资格与 AND/OR 必需技能组单测。"""
from __future__ import annotations

from kerui_recruit.match.policy import evaluate_pair
from kerui_recruit.match.service import _skill_coverage, _skill_hit


def test_and_skill_requires_both_tokens():
    # Java and Spring 是 AND：仅有 Java 或仅有 Spring 都不满足。
    assert not _skill_hit({"java"}, "Java and Spring")
    assert not _skill_hit({"spring"}, "Java and Spring")
    assert _skill_hit({"java", "spring"}, "Java and Spring")


def test_chinese_and_connector_requires_both():
    # 「Java及Spring」的「及」是 AND，不是 OR。
    assert not _skill_hit({"java"}, "Java及Spring")
    assert _skill_hit({"java", "spring"}, "Java及Spring")


def test_or_skill_requires_any_token():
    assert _skill_hit({"java"}, "Java or Kotlin")
    assert _skill_hit({"kotlin"}, "Java or Kotlin")


def test_skill_coverage_reports_missing_for_and_group():
    matched, missing = _skill_coverage(
        {"skills": ["Java"]},
        ["Java and Spring"],
    )
    assert matched == []
    assert missing == ["Java and Spring"]


def test_evaluate_pair_direction_missing_is_not_hard_filtered():
    # 方向未确认（null）不参与硬过滤：不判 pending、不拒绝。
    decision = evaluate_pair(
        {"direction": None, "required_skills": []},
        {"direction": None, "skills": []},
    )
    assert decision.eligibility == "eligible"
    assert decision.hard_reasons == ()


def test_evaluate_pair_direction_mismatch_is_rejected():
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": []},
        {"direction": "ALGORITHM", "skills": []},
    )
    assert decision.eligibility == "rejected"
    assert "career_direction_mismatch" in decision.hard_reasons


def test_career_specializations_are_compared_before_broad_directions():
    """两侧都有细分时按细分判交集：大类相同但细分不重合 → 拒绝。"""
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "career_specializations": ["BACKEND_SERVICE"],
         "required_skills": []},
        {"career_directions": ["BACKEND"], "career_specializations": ["BACKEND_AI_APPLICATION"],
         "skills": []},
    )
    assert decision.eligibility == "rejected"
    assert "career_direction_mismatch" in decision.hard_reasons


def test_career_specialization_intersection_passes():
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "career_specializations": ["BACKEND_SERVICE", "BACKEND_FULL_STACK"],
         "required_skills": []},
        {"career_directions": ["BACKEND", "DATA"], "career_specializations": ["BACKEND_FULL_STACK"],
         "skills": []},
    )
    assert decision.eligibility == "eligible"


def test_career_falls_back_to_broad_direction_when_specializations_missing():
    """存量数据没有细分：回退大类比较，保住原有的方向硬门槛。"""
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": []},
        {"direction": "BACKEND", "skills": []},
    )
    assert decision.eligibility == "eligible"


def test_legacy_specialization_vocabulary_does_not_falsely_reject():
    """旧版专长代码与新词表不可比：不得因词表不同而误判为方向不一致。"""
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "career_specializations": ["BACKEND_SERVICE"],
         "required_skills": []},
        {"career_directions": ["BACKEND"], "career_specializations": ["FULL_STACK"], "skills": []},
    )
    assert decision.eligibility == "eligible"


def test_other_direction_does_not_block_either_side():
    """OTHER 是兜底值，语义等于未知，不得参与硬淘汰。"""
    for jd_value, candidate_value in (("OTHER", "BACKEND"), ("BACKEND", "OTHER"),
                                      ("OPERATIONS", "BACKEND"), ("BACKEND", "OPERATIONS")):
        decision = evaluate_pair(
            {"direction": jd_value, "required_skills": []},
            {"direction": candidate_value, "skills": []},
        )
        assert decision.eligibility == "eligible", (jd_value, candidate_value)


def test_business_direction_mismatch_is_not_rejected():
    """业务方向不一致不再淘汰：只回传 business_match=False 供排序分层。"""
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "business_directions": ["INSURANCE"], "required_skills": []},
        {"career_directions": ["BACKEND"], "business_directions": ["GAMING"], "skills": []},
    )
    assert decision.eligibility == "eligible"
    assert decision.hard_reasons == ()
    assert decision.business_match is False


def test_business_direction_intersection_passes():
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "business_directions": ["INSURANCE", "MARKETING"],
         "required_skills": []},
        {"career_directions": ["BACKEND"], "business_directions": ["MARKETING"], "skills": []},
    )
    assert decision.eligibility == "eligible"
    assert decision.business_match is True


def test_business_direction_missing_on_one_side_is_not_filtered():
    """业务方向是新增字段：任一侧缺失时不淘汰，标记为 None 并归入非置顶组。"""
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "business_directions": ["INSURANCE"], "required_skills": []},
        {"career_directions": ["BACKEND"], "skills": []},
    )
    assert decision.eligibility == "eligible"
    assert decision.business_match is None


def test_career_mismatch_rejects_even_when_business_matches():
    """职业方向仍是硬门槛：职业不一致直接拒绝，业务方向一致也不放行。"""
    decision = evaluate_pair(
        {"career_directions": ["BACKEND"], "career_specializations": ["BACKEND_SERVICE"],
         "business_directions": ["INSURANCE"], "required_skills": []},
        {"career_directions": ["ALGORITHM"], "career_specializations": ["ALGORITHM_LLM"],
         "business_directions": ["INSURANCE"], "skills": []},
    )
    assert decision.eligibility == "rejected"
    assert "career_direction_mismatch" in decision.hard_reasons
    assert decision.business_match is True


def test_evaluate_pair_must_skill_missing_is_rejected():
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java and Spring"]},
        {"direction": "BACKEND", "skills": ["Java"]},
    )
    assert decision.eligibility == "rejected"


def test_must_groups_require_all_groups():
    # 组间 AND：两个独立必备组缺一个即拒绝（硬门槛由 must_skill_groups 表达）。
    decision = evaluate_pair(
        {"direction": "BACKEND",
         "must_skill_groups": [{"alternatives": ["Java"], "source_quote": "必须 Java"},
                               {"alternatives": ["Spring"], "source_quote": "必须 Spring"}]},
        {"direction": "BACKEND", "skills": ["Java"]},
    )
    assert decision.eligibility == "rejected"
    assert decision.missing_skills == ("Spring",)


def test_legacy_required_skills_soft_match_any():
    # 旧 required_skills 作为软特征：至少命中一项即不排除（避免泛词/可替代框架造成全库漏检）。
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java", "Spring"]},
        {"direction": "BACKEND", "skills": ["Java"]},
    )
    assert decision.eligibility == "eligible"


def test_legacy_required_skills_zero_hit_is_rejected():
    # 旧 required_skills 零命中仍排除（没有任何岗位专属技能证据）。
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java", "Spring"]},
        {"direction": "BACKEND", "skills": ["Python"]},
    )
    assert decision.eligibility == "rejected"


def test_evaluate_pair_or_group_accepts_any():
    # 同一个 OR 组满足任一即可。
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java or Kotlin"]},
        {"direction": "BACKEND", "skills": ["Kotlin"]},
    )
    assert decision.eligibility == "eligible"
    assert decision.missing_skills == ()



def test_evaluate_pair_duty_evidence_references_projects():
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java"], "core_duties": ["负责支付高并发服务"]},
        {"direction": "BACKEND", "skills": ["Java"], "projects": [{"summary": "搭建支付高并发服务"}]},
    )
    assert decision.eligibility == "eligible"
    assert "负责支付高并发服务" in decision.duty_evidence


def test_evaluate_pair_unrelated_duty_has_no_evidence():
    decision = evaluate_pair(
        {"direction": "BACKEND", "required_skills": ["Java"], "core_duties": ["负责支付高并发服务"]},
        {"direction": "BACKEND", "skills": ["Java"], "projects": [{"summary": "内容管理系统"}]},
    )
    assert decision.eligibility == "eligible"
    assert decision.duty_evidence == ()


# ---- v2 必备技能 AND/OR 组回归 ----


def test_frontend_synonym_does_not_reject_component_build_candidate():
    # “前端工程化”不能按字面硬筛：有组件库/构建工程证据即视为命中。
    decision = evaluate_pair(
        {"direction": "FRONTEND",
         "must_skill_groups": [{"alternatives": ["前端工程化"], "source_quote": "熟悉前端工程化"}]},
        {"direction": "FRONTEND", "skills": ["React", "Webpack"],
         "projects": [{"summary": "搭建组件库与构建工程"}]},
    )
    assert decision.eligibility == "eligible"


def test_relational_db_synonym_does_not_reject_mysql_candidate():
    # “关系型数据库”不能按字面硬筛：有 MySQL/SQL 即视为命中。
    decision = evaluate_pair(
        {"direction": "DATA",
         "must_skill_groups": [{"alternatives": ["关系型数据库"], "source_quote": "熟悉关系型数据库"}]},
        {"direction": "DATA", "skills": ["MySQL", "SQL"],
         "projects": [{"summary": "数仓建模"}]},
    )
    assert decision.eligibility == "eligible"


def test_must_group_or_accepts_any_alternative():
    decision = evaluate_pair(
        {"direction": "BACKEND",
         "must_skill_groups": [{"alternatives": ["LangGraph", "CrewAI"], "source_quote": "熟悉 LangGraph 或 CrewAI"}]},
        {"direction": "BACKEND", "skills": ["LangGraph"]},
    )
    assert decision.eligibility == "eligible"


def test_must_group_no_evidence_is_rejected():
    decision = evaluate_pair(
        {"direction": "BACKEND",
         "must_skill_groups": [{"alternatives": ["Java"], "source_quote": "必须熟练使用 Java"}]},
        {"direction": "BACKEND", "skills": []},
    )
    assert decision.eligibility == "rejected"

