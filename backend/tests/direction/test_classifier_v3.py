"""职业方向 v4 细分专长测试：新词表（BACKEND_* / FRONTEND_* / ALGORITHM_* / DATA_*）。"""
from __future__ import annotations

from kerui_recruit.direction.classifier import classify_specializations


def test_full_stack_requires_both_frontend_and_backend_delivery_evidence():
    spec = classify_specializations({
        "skills": ["Java", "Spring", "React", "TypeScript"],
        "experiences": [{"summary": "负责交易系统后端 API 与前端管理台开发"}],
    }, ("BACKEND",))
    assert "BACKEND_FULL_STACK" in spec


def test_skills_only_both_sides_is_not_full_stack():
    spec = classify_specializations({"skills": ["React", "Java"], "summary": ""}, ("BACKEND",))
    assert "BACKEND_FULL_STACK" not in spec


def test_tech_stack_alone_is_not_full_stack():
    """只列技术栈不算全栈交付：tech_stack 刻意不参与全栈判定。"""
    spec = classify_specializations({
        "projects": [{"name": "交易系统", "tech_stack": "React Java Spring", "summary": ""}],
    }, ("BACKEND",))
    assert "BACKEND_FULL_STACK" not in spec


def test_app_substring_does_not_fake_frontend_evidence():
    """``app`` 不得命中 application/mapping 这类子串，这是过去全栈被误判的主因。"""
    spec = classify_specializations({
        "experiences": [{"summary": "负责后端服务开发与应用发布流程 mapping 维护"}],
    }, ("BACKEND",))
    assert "BACKEND_FULL_STACK" not in spec


def test_word_boundary_still_matches_chinese_adjacent_latin_terms():
    """词边界匹配不能误伤「与中文相邻」的写法，例如 react开发 / iOS客户端。"""
    spec = classify_specializations({
        "experiences": [{"summary": "react开发与 Java服务端交付"}],
    }, ("BACKEND",))
    assert "BACKEND_FULL_STACK" in spec


def test_ai_application_is_not_algorithm_training():
    app = classify_specializations({
        "skills": ["Python", "LangGraph", "RAG"],
        "projects": [{"summary": "基于 LangGraph 的 Agent 应用接入客服系统"}],
    }, ("BACKEND",))
    assert "BACKEND_AI_APPLICATION" in app

    # 算法大类下不产出 AI 应用集成（训练/微调属于 ALGORITHM）
    training = classify_specializations({
        "skills": ["PyTorch", "SFT"],
        "projects": [{"summary": "大模型微调与离线评测"}],
    }, ("ALGORITHM",))
    assert "BACKEND_AI_APPLICATION" not in training
    assert "ALGORITHM_TRAINING" in training


def test_specializations_must_belong_to_selected_directions():
    spec = classify_specializations({
        "skills": ["Flink", "Kafka", "Doris"],
        "projects": [{"summary": "数据管道与数仓维度建模"}],
    }, ("DATA",))
    assert "DATA_ENGINEERING" in spec or "DATA_WAREHOUSE" in spec
    # 未选 BACKEND 时不得产出后端的细分
    assert not any(code.startswith("BACKEND_") for code in spec)


def test_specializations_capped_per_direction():
    spec = classify_specializations({
        "skills": ["Flink", "Kafka", "Doris", "Tableau"],
        "projects": [{"summary": "数据管道 数据采集 数仓 维度建模 指标体系 报表 BI 分析"}],
    }, ("DATA",))
    assert len(spec) == 2, spec


def test_no_directions_means_no_specializations():
    assert classify_specializations({"skills": ["Java"]}, ()) == ()
    assert classify_specializations({"skills": ["Java"]}, None) == ()
