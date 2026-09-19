"""职业方向统一规则单测：合法枚举、归一与待核处理。"""
from __future__ import annotations

from kerui_recruit.direction.policy import (
    BUSINESS_DIRECTIONS,
    CAREER_SPECIALIZATIONS,
    LEGACY_SPECIALIZATION_MAP,
    MAX_BUSINESS_DIRECTIONS,
    MAX_CAREER_DIRECTIONS,
    MAX_CAREER_DIRECTIONS_JD,
    MAX_SPECIALIZATIONS_PER_DIRECTION,
    SPECIALIZATION_PARENT,
    TAXONOMY_VERSION,
    VALID_DIRECTIONS,
    DirectionDecision,
    apply_direction_normalization,
    is_pending_career,
    is_pending_direction,
    is_valid_business_direction,
    is_valid_direction,
    is_valid_specialization,
    normalize_business_directions,
    normalize_career,
    normalize_direction,
    specialization_parent,
)


def test_valid_directions_are_accepted():
    for value in VALID_DIRECTIONS:
        assert is_valid_direction(value)
    assert is_valid_direction(None)  # 无证据允许为 null


def test_invalid_direction_is_rejected():
    assert not is_valid_direction("OPERATIONS")
    assert not is_valid_direction("ops")
    assert not is_valid_direction("backend")


def test_normalize_direction_maps_invalid_to_none():
    assert normalize_direction("BACKEND") == "BACKEND"
    assert normalize_direction("OTHER") == "OTHER"
    assert normalize_direction(None) is None
    # 非枚举值归待核，不直接入索引
    assert normalize_direction("OPERATIONS") is None
    assert normalize_direction("") is None


def test_direction_decision_carries_taxonomy_version():
    decision = DirectionDecision(direction="BACKEND", confidence="high", evidence=("summary",))
    assert decision.taxonomy_version == TAXONOMY_VERSION
    assert decision.evidence == ("summary",)


def test_is_pending_direction_covers_null_other_invalid():
    assert is_pending_direction(None)
    assert is_pending_direction("OTHER")
    assert is_pending_direction("OPERATIONS")
    assert is_pending_direction("")
    assert not is_pending_direction("BACKEND")
    assert not is_pending_direction("DATA")


def test_specialization_taxonomy_is_consistent():
    """每个细分的大类前缀必须与其映射到的大类一致，且大类必须合法。"""
    assert len(CAREER_SPECIALIZATIONS) == len(set(CAREER_SPECIALIZATIONS))
    for spec, parent in SPECIALIZATION_PARENT.items():
        assert parent in VALID_DIRECTIONS, f"{spec} 的父大类 {parent} 非法"
        assert spec.startswith(f"{parent}_"), f"{spec} 未以大类 {parent} 为前缀"
        assert is_valid_specialization(spec)


def test_legacy_specializations_map_into_new_taxonomy():
    for legacy, modern in LEGACY_SPECIALIZATION_MAP.items():
        assert is_valid_specialization(modern), f"{legacy} -> {modern} 不是合法新细分"


def test_business_directions_are_unique():
    assert len(BUSINESS_DIRECTIONS) == len(set(BUSINESS_DIRECTIONS))
    for value in BUSINESS_DIRECTIONS:
        assert is_valid_business_direction(value)
    assert not is_valid_business_direction("banking")


def test_specialization_parent_resolves_and_rejects_invalid():
    assert specialization_parent("BACKEND_SERVICE") == "BACKEND"
    assert specialization_parent("DATA_WAREHOUSE") == "DATA"
    assert specialization_parent("MANAGEMENT_TEAM") == "MANAGEMENT"
    assert specialization_parent("FULL_STACK") is None
    assert specialization_parent("NOT_A_SPEC") is None


def test_normalize_career_caps_directions_to_two():
    directions, specs = normalize_career(
        ["BACKEND", "FRONTEND", "DATA", "OPS"],
        ["BACKEND_SERVICE", "FRONTEND_WEB", "DATA_ANALYSIS", "OPS_SRE"],
    )
    assert directions == ("BACKEND", "FRONTEND")
    # 被裁掉的大类，其细分一并丢弃
    assert set(specs) == {"BACKEND_SERVICE", "FRONTEND_WEB"}


def test_normalize_career_prefers_directions_with_specializations():
    # 有细分支撑的大类优先入选，其余按输入顺序补齐到上限
    directions, specs = normalize_career(
        ["OPS", "QA", "DATA", "BACKEND"],
        ["BACKEND_SERVICE", "BACKEND_AI_APPLICATION"],
    )
    assert directions == ("BACKEND", "OPS")
    assert specs == ("BACKEND_SERVICE", "BACKEND_AI_APPLICATION")


def test_normalize_career_caps_specializations_per_direction():
    directions, specs = normalize_career(
        ["BACKEND"],
        ["BACKEND_SERVICE", "BACKEND_AI_APPLICATION", "BACKEND_FULL_STACK"],
    )
    assert directions == ("BACKEND",)
    assert specs == ("BACKEND_SERVICE", "BACKEND_AI_APPLICATION")
    assert len(specs) <= MAX_SPECIALIZATIONS_PER_DIRECTION


def test_normalize_career_drops_specializations_without_parent():
    directions, specs = normalize_career(["DATA"], ["BACKEND_SERVICE", "DATA_ANALYSIS"])
    assert directions == ("DATA",)
    assert specs == ("DATA_ANALYSIS",)


def test_normalize_career_maps_legacy_and_dedups():
    directions, specs = normalize_career(
        ["BACKEND", "DATA"],
        ["FULL_STACK", "AI_APPLICATION", "DATA_PLATFORM", "DATA_WAREHOUSE", "BACKEND_SERVICE"],
    )
    assert directions == ("BACKEND", "DATA")
    assert specs == ("BACKEND_FULL_STACK", "BACKEND_AI_APPLICATION", "DATA_ENGINEERING", "DATA_WAREHOUSE")


def test_normalize_career_ignores_invalid_and_blank():
    directions, specs = normalize_career(["backend", "", None, "OPERATIONS", "DATA"], ["", "UNKNOWN"])
    assert directions == ("DATA",)
    assert specs == ()


def test_normalize_career_accepts_scalar_input():
    assert normalize_career("BACKEND", "BACKEND_SERVICE") == (("BACKEND",), ("BACKEND_SERVICE",))
    assert normalize_career(None, None) == ((), ())


def test_normalize_business_directions_caps_and_dedups():
    assert normalize_business_directions(["INSURANCE", "MARKETING", "GAMING"]) == ("INSURANCE", "MARKETING")
    assert normalize_business_directions(["INSURANCE", "INSURANCE"]) == ("INSURANCE",)
    assert normalize_business_directions(["insurance", "UNKNOWN"]) == ()
    assert len(normalize_business_directions(BUSINESS_DIRECTIONS)) == MAX_BUSINESS_DIRECTIONS


def test_is_pending_career_covers_multi_value_forms():
    assert is_pending_career(None)
    assert is_pending_career([])
    assert is_pending_career([""])
    assert is_pending_career(["OTHER"])
    assert is_pending_career("")
    assert is_pending_career("OTHER")
    assert is_pending_career("OPERATIONS")
    assert not is_pending_career(["OTHER", "BACKEND"])
    assert not is_pending_career(["BACKEND"])
    # 过渡期容忍单值字符串
    assert not is_pending_career("BACKEND")


def test_assessment_carries_multi_value_directions():
    decision = DirectionDecision(
        direction="BACKEND",
        confidence="high",
        evidence=("projects[0].summary",),
        specializations=("BACKEND_SERVICE", "BACKEND_AI_APPLICATION"),
        career_directions=("BACKEND", "DATA"),
        business_directions=("INSURANCE", "MARKETING"),
    )
    payload = decision.assessment()
    assert payload["career_directions"] == ["BACKEND", "DATA"]
    assert payload["business_directions"] == ["INSURANCE", "MARKETING"]
    assert payload["specializations"] == ["BACKEND_SERVICE", "BACKEND_AI_APPLICATION"]
    assert payload["taxonomy_version"] == TAXONOMY_VERSION


def test_assessment_falls_back_to_single_direction():
    decision = DirectionDecision(direction="DATA", confidence="medium")
    assert decision.assessment()["career_directions"] == ["DATA"]
    assert MAX_CAREER_DIRECTIONS == 2


def test_apply_direction_normalization_mirrors_and_caps():
    """编辑接口直接改 parsed_data，必须显式归一化，否则镜像字段会与多值方向脱节。"""
    parsed = {
        "career_directions": ["BACKEND", "DATA", "OPS"],
        "career_specializations": ["BACKEND_SERVICE", "DATA_ANALYSIS", "OPS_SRE"],
        "business_directions": ["INSURANCE", "MARKETING", "GAMING"],
        "direction_assessment": {"primary": "OLD", "confidence": "low"},
    }
    apply_direction_normalization(parsed)

    assert parsed["career_directions"] == ["BACKEND", "DATA"]
    assert parsed["career_specializations"] == ["BACKEND_SERVICE", "DATA_ANALYSIS"]
    assert parsed["business_directions"] == ["INSURANCE", "MARKETING"]
    assert parsed["direction"] == "BACKEND"
    assert parsed["career_taxonomy_version"] == TAXONOMY_VERSION
    assert parsed["direction_assessment"]["career_directions"] == ["BACKEND", "DATA"]


def test_apply_direction_normalization_respects_jd_limit():
    parsed = {
        "career_directions": ["BACKEND", "DATA", "OPS"],
        "career_specializations": ["BACKEND_SERVICE", "DATA_ANALYSIS", "OPS_SRE"],
    }
    apply_direction_normalization(parsed, max_directions=MAX_CAREER_DIRECTIONS_JD)
    assert parsed["career_directions"] == ["BACKEND", "DATA", "OPS"]


def test_apply_direction_normalization_drops_other_and_invalid():
    parsed = {
        "career_directions": ["OTHER", "OPERATIONS"],
        "career_specializations": ["BACKEND_SERVICE"],
        "business_directions": ["UNKNOWN"],
    }
    apply_direction_normalization(parsed)
    assert parsed["career_directions"] == ["OTHER"]
    assert parsed["career_specializations"] == []
    assert parsed["business_directions"] == []
    # OTHER 不镜像为单值 direction，保持「待核」语义
    assert "direction" not in parsed


def test_apply_direction_normalization_is_idempotent():
    parsed = {
        "career_directions": ["BACKEND"],
        "career_specializations": ["BACKEND_SERVICE"],
        "business_directions": ["INSURANCE"],
    }
    apply_direction_normalization(parsed)
    first = dict(parsed)
    apply_direction_normalization(parsed)
    assert parsed == first
