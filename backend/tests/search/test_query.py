from kerui_recruit.search.query import normalize_skill, parse_query


def test_parse_query_extracts_years_degree_location_and_qs() -> None:
    parsed = parse_query("Java 后端 5年以上 硕士 上海 QS前100")

    assert parsed.keywords == "Java 后端"
    assert parsed.filters.min_years == 5.0
    assert parsed.filters.highest_degree == "MASTER"
    assert parsed.filters.location == "上海"
    assert parsed.filters.max_qs_rank == 100


def test_parse_query_returns_empty_filters_for_plain_keywords() -> None:
    parsed = parse_query("Python 金融风控")

    assert parsed.keywords == "Python 金融风控"
    assert parsed.filters.min_years is None
    assert parsed.filters.highest_degree is None
    assert parsed.filters.location is None
    assert parsed.filters.max_qs_rank is None


def test_parse_query_maps_degree_alias_to_normalized_form() -> None:
    parsed = parse_query("数据分析 本科")

    assert parsed.filters.highest_degree == "BACHELOR"
    assert "本科" not in parsed.keywords


# --- 年限范围与毕业年份 ---


def test_range_years_3_to_5() -> None:
    parsed = parse_query("Java 3-5年")

    assert parsed.filters.min_years == 3.0
    assert parsed.filters.max_years == 5.0


def test_max_years_within() -> None:
    parsed = parse_query("3年以内")

    assert parsed.filters.min_years is None
    assert parsed.filters.max_years == 3.0


def test_min_years_at_least() -> None:
    parsed = parse_query("至少3年")

    assert parsed.filters.min_years == 3.0
    assert parsed.filters.max_years is None


def test_graduation_year_is_not_experience() -> None:
    parsed = parse_query("2020年毕业 硕士")

    assert parsed.filters.min_years is None
    assert parsed.filters.max_years is None


# --- 学历层级 ---


def test_degree_and_above_is_inclusive() -> None:
    parsed = parse_query("本科及以上")

    assert parsed.filters.highest_degree == "BACHELOR"
    assert parsed.filters.degree_exact is False


def test_degree_exact_only_bachelor() -> None:
    parsed = parse_query("仅本科")

    assert parsed.filters.highest_degree == "BACHELOR"
    assert parsed.filters.degree_exact is True


def test_degree_priority_is_not_hard_filter() -> None:
    parsed = parse_query("学历不限，本科优先")

    assert parsed.filters.highest_degree is None


# --- 多地点与现居/意向 ---


def test_multi_location_or() -> None:
    parsed = parse_query("上海或北京 Java")

    assert set(parsed.filters.locations) == {"上海", "北京"}


def test_preferred_location_separated() -> None:
    parsed = parse_query("现居上海，期望北京 Java")

    assert parsed.filters.location == "上海"
    assert parsed.filters.preferred_location == "北京"


# --- 否定与排除 ---


def test_soft_negation_not_positive() -> None:
    parsed = parse_query("不要求Java Python")

    assert "Java" not in parsed.keywords
    assert parsed.filters.exclude_skills == ()


def test_exclusion_becomes_exclude_skill() -> None:
    parsed = parse_query("排除Java Python")

    assert parsed.filters.exclude_skills == ("Java",)


# --- 纯过滤查询 ---


def test_pure_filter_query_has_empty_keywords() -> None:
    parsed = parse_query("硕士 上海")

    assert parsed.keywords == ""


# --- 技能标准化 ---


def test_normalize_skill_keeps_java_distinct_from_javascript() -> None:
    assert normalize_skill("java") == "Java"
    assert normalize_skill("JAVA") == "Java"
    assert normalize_skill("JavaScript") == "JavaScript"
    assert normalize_skill("java") != normalize_skill("javascript")


def test_normalize_skill_preserves_cpp_and_csharp() -> None:
    assert normalize_skill("C++") == "C++"
    assert normalize_skill("C#") == "C#"


# --- 回归：本次修复的边界问题 ---


def test_graduation_prefix_year_is_not_experience() -> None:
    parsed = parse_query("毕业于2020年 Java")

    assert parsed.filters.min_years is None
    assert parsed.filters.max_years is None
    assert "Java" in parsed.keywords


def test_required_degree_and_above_is_inclusive() -> None:
    parsed = parse_query("必须本科及以上 Java")

    assert parsed.filters.highest_degree == "BACHELOR"
    assert parsed.filters.degree_exact is False


def test_preferred_multi_location_is_not_residence() -> None:
    parsed = parse_query("期望上海或北京 Java")

    assert set(parsed.filters.preferred_locations) == {"上海", "北京"}
    assert "北京" not in parsed.filters.locations
    assert "上海" not in parsed.filters.locations
    assert parsed.filters.location is None


def test_soft_negation_degree_not_hard_filter() -> None:
    parsed = parse_query("不要求硕士 Java")

    assert parsed.filters.highest_degree is None
    assert "Java" in parsed.keywords


def test_exclusion_in_pure_filter_query() -> None:
    parsed = parse_query("硕士 排除Java")

    assert parsed.keywords == ""
    assert parsed.filters.exclude_skills == ("Java",)


def test_preferred_locations_before_residence_are_kept_separate() -> None:
    parsed = parse_query("意向深圳或广州，现居上海 Python")
    assert parsed.filters.location_values() == ("上海",)
    assert parsed.filters.preferred_location_values() == ("深圳", "广州")
    assert parsed.keywords == "Python"


def test_parse_query_returns_filters_keywords_and_concepts() -> None:
    parsed = parse_query("JS 后端 5年以上 上海")
    assert parsed.keywords == "JS 后端"
    assert [concept.canonical for concept in parsed.concepts] == ["JavaScript", "后端"]
    assert parsed.filters.min_years == 5
    assert parsed.filters.location == "上海"


# --- Task 2：学校实体保护，避免城市子串误判为硬地点 ---


def test_school_entity_peking_is_not_location() -> None:
    parsed = parse_query("北京大学 后端")
    assert parsed.filters.location is None
    assert parsed.filters.locations == ()
    assert "北京大学" in parsed.keywords


def test_school_entity_shanghai_jiaotong_is_not_location() -> None:
    parsed = parse_query("上海交通大学 Java")
    assert parsed.filters.location is None
    assert parsed.filters.locations == ()
    assert "上海交通大学" in parsed.keywords


def test_explicit_residence_still_produces_location() -> None:
    parsed = parse_query("现居北京 后端")
    assert parsed.filters.location == "北京"


# --- 城市词典扩容（2026-09-21）：地级市 + 组合写法 ---


def test_prefecture_level_city_is_parsed_as_location() -> None:
    parsed = parse_query("佛山 后端")
    assert parsed.filters.location == "佛山"
    assert "佛山" not in parsed.keywords


def test_city_group_expands_into_multiple_locations() -> None:
    parsed = parse_query("北上广深 后端")
    assert set(parsed.filters.locations) == {"北京", "上海", "广州", "深圳"}
    assert parsed.filters.location == "北京"
    assert "北上广深" not in parsed.keywords


def test_province_prefix_and_city_suffix_are_stripped_together() -> None:
    """「广东省深圳市」要一次吃掉省级前缀与「市」后缀，不能留下「广东省」「市」这类垃圾词条。"""
    parsed = parse_query("现居广东省深圳市 算法")
    assert parsed.filters.location == "深圳"
    assert parsed.keywords == "算法"


def test_district_only_writing_is_not_a_location_condition() -> None:
    """「朝阳区」语义不确定（北京朝阳 / 辽宁朝阳），不产生地点条件，原词留在词条里。"""
    parsed = parse_query("朝阳区 算法工程师")
    assert parsed.filters.location is None
    assert parsed.filters.locations == ()
    assert "朝阳区" in parsed.keywords


def test_school_entity_with_province_name_is_preserved() -> None:
    parsed = parse_query("广东外语外贸大学 英语")
    assert parsed.filters.location is None
    assert "广东外语外贸大学" in parsed.keywords


# --- Task 2：排除对象不能被静默忽略 ---


def test_chinese_exclude_concept_is_recognized() -> None:
    parsed = parse_query("排除外包 Java")
    assert parsed.filters.exclude_skills == ("外包",)
    assert "外包" not in parsed.keywords
    assert "排除" not in parsed.keywords
    assert "Java" in parsed.keywords
