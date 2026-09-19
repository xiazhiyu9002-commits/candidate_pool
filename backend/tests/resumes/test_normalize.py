from decimal import Decimal

from kerui_recruit.resumes.normalize import derive_education_compat, normalize_resume
from kerui_recruit.resumes.structured import ParsedEducation, ParsedExperience, ParsedResume, ProfilePoint


def test_resume_normalization_trims_deduplicates_and_maps_enums() -> None:
    """Unnormalized provider spellings would split filters and corrupt matching."""
    normalized = normalize_resume(
        ParsedResume(
            name=" 张三 ",
            total_years=5.04,
            highest_degree="硕士",
            skills=["Python", "python", " 金融风控 "],
            summary="  金融科技后端工程师  ",
            experiences=[
                ParsedExperience(company=" 示例科技 ", title=" 后端工程师 ", summary=" 风控 ")
            ],
        )
    )

    assert normalized.name == "张三"
    assert normalized.total_years == Decimal("5.0")
    assert normalized.highest_degree == "MASTER"
    assert normalized.skills == ("Python", "金融风控")
    assert normalized.summary == "金融科技后端工程师"
    assert normalized.experiences[0].company == "示例科技"


def test_resume_normalization_passes_school_qs_graduation_and_industry() -> None:
    """School tier and QS ranking must survive normalization for hard filters."""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            highest_degree="硕士",
            school=" 清华大学 ",
            qs_rank=25,
            graduation_year=2018,
            industry=" 互联网 ",
        )
    )

    assert normalized.school == "清华大学"
    assert normalized.qs_rank == 25
    assert normalized.graduation_year == 2018
    assert normalized.industry == "互联网"


def test_resume_normalization_preserves_multiple_educations() -> None:
    """Structured educations must survive normalization for multi-education display."""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            educations=[
                ParsedEducation(school="北京大学", degree="硕士", major="计算机",
                                graduation_year=2020, school_tags=["985", "211", "双一流"]),
                ParsedEducation(school="清华大学", degree="本科", major="软件工程",
                                graduation_year=2016, school_tags=["985"]),
            ],
        )
    )

    assert len(normalized.educations) == 2
    assert normalized.educations[0].school == "北京大学"
    assert normalized.educations[0].degree == "MASTER"
    assert normalized.educations[0].school_tags == ("985", "211", "双一流")
    assert normalized.educations[1].school == "清华大学"
    assert normalized.educations[1].degree == "BACHELOR"


def test_derive_education_compat_recomputes_compat_fields() -> None:
    compat = derive_education_compat([
        {"school": "清华大学", "degree": "本科", "graduation_year": 2016,
         "school_tags": ["985", "211", "双一流"]},
        {"school": "北京大学", "degree": "硕士", "graduation_year": 2020,
         "school_tags": ["985"], "qs_rank": 30},
    ])

    assert compat["highest_degree"] == "MASTER"
    assert compat["school"] == "北京大学"
    assert compat["graduation_year"] == 2016  # 本科毕业年份优先
    assert compat["school_level"] == "985"
    assert compat["qs_rank"] == 30


def test_manual_current_company_title_wins_over_experience() -> None:
    """人工覆盖的 current_company/current_title 必须优先于最近工作经历派生。"""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            current_company="人工新公司",
            current_title="人工新职位",
            experiences=[
                ParsedExperience(company="解析旧公司", title="解析旧职位", end_date="至今"),
            ],
        )
    )

    assert normalized.current_company == "人工新公司"
    assert normalized.current_title == "人工新职位"


def test_current_company_title_derives_from_recent_experience_when_absent() -> None:
    """没有显式 current_company/current_title 时，才从最近工作经历派生。"""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            experiences=[
                ParsedExperience(company="派生公司", title="派生职位", end_date="至今"),
            ],
        )
    )

    assert normalized.current_company == "派生公司"
    assert normalized.current_title == "派生职位"


def test_normalization_preserves_ai_profile_metadata() -> None:
    """标准化不得丢失 AI 画像的 source / input_hash / stale 元数据。"""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            ai_profile_summary="画像正文",
            ai_profile_source="ai",
            ai_profile_input_hash="abc123",
            ai_profile_stale=True,
        )
    )

    assert normalized.ai_profile_summary == "画像正文"
    assert normalized.ai_profile_source == "ai"
    assert normalized.ai_profile_input_hash == "abc123"
    assert normalized.ai_profile_stale is True


def test_normalization_preserves_dual_form_profile() -> None:
    """标准化须保留双形态画像的 narrative / points / compact，且 narrative 缺省回退 summary（同源）。"""
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            ai_profile_summary="整体段落",
            ai_profile_points=[ProfilePoint(text="  分点一  ", evidence_paths=["experiences[0].summary"])],
            ai_profile_compact="  浓缩  ",
        )
    )
    assert normalized.ai_profile_narrative == "整体段落"
    assert [p.text for p in normalized.ai_profile_points] == ["分点一"]
    assert normalized.ai_profile_points[0].evidence_paths == ["experiences[0].summary"]
    assert normalized.ai_profile_compact == "浓缩"


def test_normalization_narrative_falls_back_to_summary() -> None:
    normalized = normalize_resume(ParsedResume(name="张三", ai_profile_summary="画像正文"))
    assert normalized.ai_profile_narrative == "画像正文"
    assert normalized.ai_profile_points == []


def test_parsed_resume_caps_career_directions_and_mirrors_single_direction() -> None:
    """大类超限须截断，且单值 direction 由多值方向镜像而来，保证旧字段可用。"""
    parsed = ParsedResume(
        name="张三",
        career_directions=["BACKEND", "DATA", "OPS"],
        career_specializations=["BACKEND_SERVICE", "DATA_ANALYSIS", "OPS_SRE"],
    )
    assert parsed.career_directions == ["BACKEND", "DATA"]
    assert parsed.career_specializations == ["BACKEND_SERVICE", "DATA_ANALYSIS"]
    assert parsed.direction == "BACKEND"


def test_parsed_resume_caps_specializations_per_direction() -> None:
    parsed = ParsedResume(
        name="张三",
        career_directions=["BACKEND"],
        career_specializations=["BACKEND_SERVICE", "BACKEND_AI_APPLICATION", "BACKEND_FULL_STACK"],
    )
    assert parsed.career_specializations == ["BACKEND_SERVICE", "BACKEND_AI_APPLICATION"]


def test_parsed_resume_maps_legacy_specializations() -> None:
    """旧专长代码映射为新细分；父大类必须在已选大类内，否则丢弃。"""
    parsed = ParsedResume(
        name="张三",
        direction="BACKEND",
        career_directions=["BACKEND", "DATA"],
        career_specializations=["FULL_STACK", "AI_APPLICATION", "DATA_PLATFORM"],
    )
    assert parsed.career_specializations == [
        "BACKEND_FULL_STACK", "BACKEND_AI_APPLICATION", "DATA_ENGINEERING",
    ]


def test_parsed_resume_drops_specializations_whose_parent_is_not_selected() -> None:
    """细分不得脱离其父大类存在，避免出现「大类=后端 但细分=数仓」这种不一致。"""
    parsed = ParsedResume(
        name="张三",
        career_directions=["BACKEND"],
        career_specializations=["DATA_WAREHOUSE"],
    )
    assert parsed.career_directions == ["BACKEND"]
    assert parsed.career_specializations == []


def test_parsed_resume_coerces_scalar_and_caps_business_directions() -> None:
    """LLM 偶尔返回标量字符串；业务方向上限为 2，非法值应被丢弃。"""
    parsed = ParsedResume(
        name="张三",
        career_directions="BACKEND",
        career_specializations="BACKEND_SERVICE",
        business_directions=["INSURANCE", "MARKETING", "GAMING", "不存在的方向"],
    )
    assert parsed.career_directions == ["BACKEND"]
    assert parsed.career_specializations == ["BACKEND_SERVICE"]
    assert parsed.business_directions == ["INSURANCE", "MARKETING"]


def test_parsed_resume_stamps_taxonomy_version_and_syncs_assessment() -> None:
    parsed = ParsedResume(
        name="张三",
        career_directions=["DATA"],
        career_specializations=["DATA_WAREHOUSE"],
        business_directions=["BANKING"],
        direction_assessment={"primary": "DATA", "confidence": "high", "taxonomy_version": "2"},
    )
    assert parsed.career_taxonomy_version == "4"
    assert parsed.direction_assessment["specializations"] == ["DATA_WAREHOUSE"]
    assert parsed.direction_assessment["career_directions"] == ["DATA"]
    assert parsed.direction_assessment["business_directions"] == ["BANKING"]


def test_parsed_resume_without_multi_directions_keeps_single_direction() -> None:
    """存量/旧提示词路径：只给单值 direction 时不得被覆盖。"""
    parsed = ParsedResume(name="张三", direction="ALGORITHM")
    assert parsed.direction == "ALGORITHM"
    assert parsed.career_directions == []
    assert parsed.business_directions == []


def test_normalize_resume_passes_multi_value_directions() -> None:
    normalized = normalize_resume(
        ParsedResume(
            name="张三",
            career_directions=["BACKEND", "DATA"],
            career_specializations=["BACKEND_SERVICE", "DATA_ANALYSIS"],
            business_directions=["INSURANCE", "MARKETING"],
        )
    )
    assert normalized.career_directions == ("BACKEND", "DATA")
    assert normalized.career_specializations == ("BACKEND_SERVICE", "DATA_ANALYSIS")
    assert normalized.business_directions == ("INSURANCE", "MARKETING")
    assert normalized.direction == "BACKEND"
    assert normalized.career_taxonomy_version == "4"


def test_normalize_resume_defaults_multi_value_directions_to_empty() -> None:
    normalized = normalize_resume(ParsedResume(name="张三"))
    assert normalized.career_directions == ()
    assert normalized.career_specializations == ()
    assert normalized.business_directions == ()
