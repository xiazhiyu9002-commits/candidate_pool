"""JD 显式硬条件解析与评估测试。"""
from __future__ import annotations

import pytest

from kerui_recruit.jd.profile_constraints import (
    evaluate_exact_constraints,
    normalize_constraints,
    parse_exact_constraints,
)
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.match.policy import evaluate_pair


def test_parse_explicit_must_conditions():
    constraints = parse_exact_constraints("卡985，字节或阿里背景，LangGraph 优先")
    kinds = {(c.kind, c.strength) for c in constraints}
    assert ("school_level", "MUST") in kinds
    assert ("company_history", "MUST") in kinds
    assert ("skill", "PLUS") in kinds
    # 优先条件不得升级为 MUST。
    skill = next(c for c in constraints if c.kind == "skill")
    assert skill.strength == "PLUS"


def test_evaluate_must_school_and_company():
    constraints = parse_exact_constraints("卡985，字节或阿里背景")
    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "211",
        "current_company": "某公司",
        "experiences": [{"company": "某公司"}],
    })
    assert len(unmet) == 2

    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "985",
        "current_company": "阿里巴巴",
        "experiences": [],
    })
    assert unmet == []


def test_plus_is_not_hard_reject():
    constraints = parse_exact_constraints("LangGraph 优先")
    unmet, _ = evaluate_exact_constraints(constraints, {"skills": ["Python"]})
    assert unmet == []


def test_evaluate_pair_rejects_unmet_must_constraint():
    decision = evaluate_pair(
        {"direction": "BACKEND", "exact_constraints": [
            {"kind": "school_level", "operator": "OR", "alternatives": ["985"], "strength": "MUST", "source": "manual", "source_text": "卡985"},
        ]},
        {"direction": "BACKEND", "school_level": "普本", "skills": ["Java"]},
    )
    assert decision.eligibility == "rejected"
    assert any("exact_constraint" in r for r in decision.hard_reasons)


def test_evaluate_pair_passes_when_must_constraint_met():
    decision = evaluate_pair(
        {"direction": "BACKEND", "exact_constraints": [
            {"kind": "school_level", "operator": "OR", "alternatives": ["985"], "strength": "MUST", "source": "manual", "source_text": "卡985"},
        ]},
        {"direction": "BACKEND", "school_level": "985", "skills": ["Java"]},
    )
    assert decision.eligibility != "rejected"


def test_985_priority_stays_plus_even_with_must_elsewhere():
    # 「必须熟悉Java」里的「必须」不应把「985优先」升级为 MUST。
    constraints = parse_exact_constraints("985优先，必须熟悉Java")
    school = next(c for c in constraints if c.kind == "school_level")
    assert school.strength == "PLUS"
    assert school.alternatives == ("985",)


def test_company_background_priority_is_plus():
    constraints = parse_exact_constraints("字节或阿里背景优先")
    company = next(c for c in constraints if c.kind == "company_history")
    assert company.strength == "PLUS"
    assert company.alternatives == ("字节", "阿里")


def test_local_must_marker_still_rejects():
    # 局部「卡」仍是 MUST；局部「必须」出现在同一语句时也是 MUST。
    constraints = parse_exact_constraints("卡985，必须熟悉Java")
    school = next(c for c in constraints if c.kind == "school_level")
    assert school.strength == "MUST"


def test_plus_985_and_company_do_not_reject_nonmatching_candidate():
    constraints = parse_exact_constraints("985优先，字节或阿里背景优先")
    unmet, _ = evaluate_exact_constraints(constraints, {
        "school_level": "普本",
        "current_company": "某公司",
        "experiences": [{"company": "某公司"}],
    })
    # PLUS 条件不硬拒。
    assert unmet == []


def test_normalize_constraints_rejects_invalid_and_unbacked_must():
    """非法取值整条丢弃；没有原文依据的 MUST 降级为 PLUS（MUST 会直接淘汰候选人）。"""
    normalized = normalize_constraints([
        {"kind": "school_level", "alternatives": ["985"], "strength": "MUST", "source_text": "卡 985"},
        {"kind": "skill", "alternatives": ["Java"], "strength": "MUST"},
        {"kind": "能力", "alternatives": ["沟通"], "strength": "MUST", "source_text": "沟通"},
        {"kind": "skill", "alternatives": ["Go"], "strength": "MAYBE", "source_text": "Go"},
        {"kind": "skill", "alternatives": [], "strength": "PLUS"},
        {"kind": "skill", "alternatives": ["Java"], "strength": "MUST", "source_text": "必须熟悉 Java"},
    ])

    assert [(c["kind"], c["strength"]) for c in normalized] == [
        ("school_level", "MUST"),
        ("skill", "PLUS"),
        ("skill", "MUST"),
    ]
    assert normalized[0]["source"] == "inferred"


def test_parsed_jd_normalizes_model_produced_constraints():
    """AI 产出的硬条件与人工编辑共用同一套规整：非法丢弃、无依据的 MUST 降级。"""
    parsed = ParsedJd(title="后端工程师", exact_constraints=[
        {"kind": "school_level", "alternatives": ["985"], "strength": "MUST"},
        {"kind": "无法识别", "alternatives": ["x"], "strength": "MUST", "source_text": "x"},
    ])

    assert len(parsed.exact_constraints) == 1
    assert parsed.exact_constraints[0].kind == "school_level"
    assert parsed.exact_constraints[0].strength == "PLUS"


class _RecordingJdClient:
    """只记录提示词并返回固定结构的 JD 任务客户端。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, response_model, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return response_model(title="后端工程师", exact_constraints=[
            {"kind": "school_level", "alternatives": ["985"], "strength": "MUST"},
            {"kind": "skill", "alternatives": ["Java"], "strength": "MUST", "source_text": "必须熟悉 Java"},
        ])


@pytest.mark.asyncio
async def test_ai_jd_parser_asks_for_constraints_with_the_shared_spec():
    """解析期就要产出硬条件（零额外调用），口径来自共享常量而非另抄一份。"""
    from kerui_recruit.providers.generation_tasks import AiJdParser

    client = _RecordingJdClient()
    parsed = await AiJdParser(client).parse_jd("必须熟悉 Java")

    prompt = client.prompts[0]
    assert "exact_constraints" in prompt
    assert "只有原文明确写" in prompt
    assert "985 优先，必须熟悉 Java" in prompt
    # 无原文依据的 MUST 已降级为 PLUS。
    assert [(c.kind, c.strength) for c in parsed.exact_constraints] == [
        ("school_level", "PLUS"),
        ("skill", "MUST"),
    ]
