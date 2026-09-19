"""职业方向确定性分类单测：主职责与项目证据优先，边界样本保守回退。"""
from __future__ import annotations

from kerui_recruit.direction.classifier import classify_direction


def test_data_pipeline_and_platform_is_backend():
    decision = classify_direction({
        "skills": ["Flink", "Kafka"],
        "summary": "数据平台与数据管道开发",
        "projects": [{"summary": "搭建实时数据管道"}],
    })
    assert decision.direction == "BACKEND"


def test_warehouse_bi_is_data():
    decision = classify_direction({
        "skills": ["SQL", "Tableau"],
        "summary": "数仓分析与 BI 报表",
    })
    assert decision.direction == "DATA"


def test_model_training_is_algorithm():
    decision = classify_direction({
        "skills": ["PyTorch"],
        "summary": "模型训练与算法研发",
    })
    assert decision.direction == "ALGORITHM"


def test_agent_orchestration_is_backend():
    decision = classify_direction({
        "skills": ["LangChain", "FastAPI"],
        "summary": "Agent 工具编排与 API 服务工程",
    })
    assert decision.direction == "BACKEND"


def test_ai_only_is_null():
    decision = classify_direction({"skills": ["AI"], "summary": "AI"})
    assert decision.direction is None
    assert decision.confidence == "low"


def test_no_evidence_is_null():
    decision = classify_direction({"skills": [], "summary": ""})
    assert decision.direction is None
    assert decision.confidence == "low"


def test_jd_parsed_data_is_scored_not_ignored():
    """JD 字段名与简历不同：必须映射后再打分，否则岗位侧方向全部判空、硬门槛整体失效。

    这是实测发现的缺陷：40 个真实 OPEN JD 曾全部判空，因为 core_duties / candidate_profile
    不是打分器认识的字段。
    """
    decision = classify_direction({
        "title": "资深 Java 后端开发工程师",
        "core_duties": ["负责微服务架构设计与高并发交易系统开发", "负责服务治理与中间件建设"],
        "required_skills": ["Java", "Spring Cloud", "MySQL"],
        "plus_skills": ["Kafka"],
        "candidate_profile": "熟悉微服务与高并发的服务端工程师",
        "industry": "银行/金融科技",
    })
    assert decision.direction == "BACKEND"
    assert "BACKEND" in decision.career_directions
    assert decision.specializations
    assert decision.business_directions == ("BANKING",)


def test_jd_without_resume_fields_uses_duties_as_primary_evidence():
    """JD 没有 experiences/projects，核心职责必须顶上主证据位（权重 50），而不是被判空。"""
    decision = classify_direction({
        "title": "数据开发工程师",
        "core_duties": ["负责数仓维度建模与指标体系搭建"],
        "required_skills": ["SQL", "Doris"],
    })
    assert decision.career_directions
    assert "DATA" in decision.career_directions
    assert any(code.startswith("DATA_") for code in decision.specializations)


def test_resume_shape_is_not_remapped_by_jd_view():
    """简历数据含 experiences，不得被 JD 映射分支误伤。"""
    decision = classify_direction({
        "title": "后端工程师",
        "core_duties": ["负责银行系统"],
        "experiences": [{"summary": "微服务与高并发交易系统开发"}],
        "skills": ["Java"],
    })
    assert decision.direction in ("BACKEND", "OTHER")
    assert decision.career_directions
