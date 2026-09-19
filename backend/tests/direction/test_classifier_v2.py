"""职业方向 v2 分类边界测试（主/次方向、置信度、证据路径、管理属性）。

这些用例对应语义盲评报告与 v2 计划中确认的边界：数据平台/API vs 数仓建模、
Agent 编排 vs 模型训练、iOS 客户端 vs 后端、研发负责人 vs 管理。
"""
from __future__ import annotations

from kerui_recruit.direction.classifier import classify_direction


def _decision(data: dict):
    return classify_direction(data)


def test_data_platform_api_and_warehouse_terms_is_backend_primary_data_secondary():
    decision = _decision({
        "summary": "负责数据平台 API 服务开发",
        "skills": ["Flink", "Kafka", "SQL"],
        "experiences": [
            {"title": "数据平台工程师", "summary": "负责数据采集处理服务与 API 开发，也涉及数仓主题建模"},
        ],
        "projects": [
            {"summary": "搭建数据平台 API 服务"},
            {"summary": "数仓指标体系构建"},
        ],
    })
    assert decision.direction == "BACKEND"
    assert decision.secondary == "DATA"


def test_warehouse_modeling_and_metrics_is_data():
    decision = _decision({
        "summary": "数仓主题建模与指标体系",
        "skills": ["SQL", "Hive", "Tableau"],
        "experiences": [{"title": "数据分析师", "summary": "负责数仓主题建模、指标体系和 BI 报表"}],
    })
    assert decision.direction == "DATA"


def test_agent_tool_orchestration_is_backend():
    decision = _decision({
        "summary": "Agent 工具编排",
        "skills": ["LangGraph", "Python"],
        "experiences": [{"title": "后端工程师", "summary": "负责 Agent 应用编排与工具调用"}],
    })
    assert decision.direction == "BACKEND"


def test_model_training_and_evaluation_is_algorithm():
    decision = _decision({
        "summary": "模型训练与评测",
        "skills": ["PyTorch"],
        "experiences": [{"title": "算法工程师", "summary": "负责模型训练、微调与离线评测"}],
    })
    assert decision.direction == "ALGORITHM"


def test_ios_client_sdk_is_frontend():
    decision = _decision({
        "summary": "iOS 客户端 SDK 开发",
        "skills": ["Objective-C", "Swift", "WebRTC"],
        "experiences": [{"title": "iOS 开发工程师", "summary": "负责 iOS 音视频 SDK 架构与性能优化"}],
    })
    assert decision.direction == "FRONTEND"


def test_rd_lead_with_java_is_backend_management():
    decision = _decision({
        "summary": "Java 架构与团队管理",
        "skills": ["Java", "Spring"],
        "experiences": [{"title": "研发负责人", "summary": "负责 Java 架构设计与团队管理，主导后端交付"}],
    })
    assert decision.direction == "BACKEND"
    assert decision.management is True


def test_skills_only_is_low_confidence():
    decision = _decision({"skills": ["Java", "Spring"], "summary": ""})
    assert decision.confidence == "low"


def test_evidence_paths_reference_actual_fields():
    decision = _decision({
        "summary": "负责交易系统后端 API 开发",
        "skills": ["Java"],
        "experiences": [{"title": "后端工程师", "summary": "负责交易系统与支付服务开发"}],
        "projects": [{"summary": "搭建高并发交易系统"}],
    })
    assert decision.direction == "BACKEND"
    assert any(path.startswith("experiences[0]") for path in decision.evidence)
    assert any(path.startswith("projects[0]") for path in decision.evidence)
