"""解析质量抽检校验器的单元测试（阶段 3 验收口径）。"""
from kerui_recruit.bench.parse_quality import (
    CandidateAttrs,
    audit_candidate,
    audit_conditions,
    audit_hard_filters,
    condition_selectivity,
    condition_values,
    percentile,
    satisfies_filters,
    summarize_plans,
    summarize_violations,
)
from kerui_recruit.search.contracts import CandidateFilters


def kinds(violations) -> list[str]:
    return [item.kind for item in violations]


# --- 原文依据（span 硬校验） ---


def test_entity_condition_must_appear_verbatim_in_query():
    assert kinds(audit_conditions("招个 Python 后端", [("name", "张三")])) == ["span_missing"]
    assert audit_conditions("张三 Python", [("name", "张三")]) == []


def test_phone_must_be_a_complete_verbatim_number():
    # 完整 11 位且在原文：通过；带分隔符也能过，因为只比较数字。
    assert audit_conditions("13800138000 后端", [("phone", "13800138000")]) == []
    assert audit_conditions("联系 138 0013 8000", [("phone", "13800138000")]) == []
    # 位数不足、或原文根本没有：都算误判。
    assert kinds(audit_conditions("1380013800 后端", [("phone", "1380013800")])) == ["phone_not_verbatim"]
    assert kinds(audit_conditions("Java 后端", [("phone", "13800138000")])) == ["phone_not_verbatim"]


# --- 词表依据（城市 / 方向枚举） ---


def test_city_condition_must_be_in_the_dictionary():
    assert audit_conditions("上海 后端", [("locations", "上海")]) == []
    assert kinds(audit_conditions("浦东 后端", [("locations", "浦东")])) == ["city_unknown"]


def test_singleton_field_aliases_are_canonicalized():
    """规则解析产物用单值字段名 location；城市检查不能被别名绕过。"""
    assert kinds(audit_conditions("浦东 后端", [("location", "浦东")])) == ["city_unknown"]
    assert audit_conditions("上海 后端", [("location", "上海")]) == []


def test_list_conditions_are_split_before_checking():
    assert condition_values("locations", "上海、北京") == ("上海", "北京")
    assert condition_values("min_years", "8.0") == ("8.0",)
    # 一个真城市 + 一个不在词典里的写法：只报后者。
    assert kinds(audit_conditions("上海 北京", [("locations", "上海、上海自贸区")])) == ["city_unknown"]


def test_direction_condition_must_be_corroborated_by_the_word_list():
    # A+C 交叉验证的 C 侧：查询里有 RAG，BACKEND_AI_APPLICATION 的细分词表能反查到。
    assert audit_conditions("RAG 应用开发", [("career_specializations", "BACKEND_AI_APPLICATION")]) == []
    # 查询里没有任何方向线索时，产出方向条件即为误判。
    assert kinds(audit_conditions("招个靠谱的人", [
        ("career_directions", "FRONTEND"),
    ])) == ["direction_unverified"]


def test_direction_exact_label_in_query_counts_as_evidence():
    """A 有 C 无时原文出现精确标签即算有依据（BACKEND_FULL_STACK 的词项是运行时合成的）。"""
    assert audit_conditions("需要全栈交付能力", [
        ("career_specializations", "BACKEND_FULL_STACK"),
    ]) == []
    assert kinds(audit_conditions("招个靠谱的人", [
        ("career_specializations", "BACKEND_FULL_STACK"),
    ])) == ["direction_unverified"]


# --- 硬条件执行复核 ---


def test_hard_filter_violations_are_detected():
    filters = CandidateFilters(min_years=5.0, highest_degree="MASTER", locations=("上海",))
    attrs = CandidateAttrs(total_years=3.0, highest_degree="BACHELOR", locations=("上海",))
    assert sorted(kinds(audit_hard_filters(filters, attrs))) == ["hard_filter"] * 2
    ok = CandidateAttrs(total_years=6.0, highest_degree="MASTER", locations=("上海",))
    assert audit_hard_filters(filters, ok) == []


def test_unknown_attributes_are_not_counted_as_violations():
    """属性缺失记为不可判：不能把「不知道」当成「违反」。"""
    filters = CandidateFilters(min_years=5.0, locations=("上海",))
    assert audit_hard_filters(filters, CandidateAttrs()) == []


def test_location_filter_uses_normalized_terms():
    """现居过滤走归一化后的 location_terms：广东省深圳市 归一为 深圳 后能命中。"""
    filters = CandidateFilters(locations=("深圳",))
    assert audit_hard_filters(filters, CandidateAttrs(locations=("深圳",))) == []
    violated = audit_hard_filters(filters, CandidateAttrs(locations=("北京",)))
    assert kinds(violated) == ["hard_filter"]


def test_degree_exact_and_above_semantics():
    above = CandidateFilters(highest_degree="BACHELOR")
    assert audit_hard_filters(above, CandidateAttrs(highest_degree="MASTER")) == []
    assert kinds(audit_hard_filters(above, CandidateAttrs(highest_degree="ASSOCIATE"))) == ["hard_filter"]
    exact = CandidateFilters(highest_degree="BACHELOR", degree_exact=True)
    assert kinds(audit_hard_filters(exact, CandidateAttrs(highest_degree="MASTER"))) == ["hard_filter"]


def test_exclude_skills_and_company_are_checked():
    filters = CandidateFilters(exclude_skills=("外包",), company="腾讯")
    attrs = CandidateAttrs(skills_text="java 外包 支付", company_text="字节跳动")
    assert sorted(kinds(audit_hard_filters(filters, attrs))) == ["hard_filter"] * 2


def test_text_fields_use_substring_semantics_like_the_index():
    """姓名/公司/职位/学校在索引侧是 LIKE 子串匹配：探针不能比生产更严。"""
    filters = CandidateFilters(title="全栈工程师")
    # 候选人的职位是「资深全栈工程师」：子串命中，不算违反。
    assert audit_hard_filters(filters, CandidateAttrs(title_text="资深全栈工程师")) == []
    assert kinds(audit_hard_filters(filters, CandidateAttrs(title_text="java 后端"))) == ["hard_filter"]


def test_school_level_uses_the_inclusive_hierarchy():
    """学校等级与学历一样是包含式层级：985 应满足 211,「双一流」也应满足。"""
    attrs = CandidateAttrs(school_tags=("985",))
    assert audit_hard_filters(CandidateFilters(school_level="211"), attrs) == []
    assert kinds(audit_hard_filters(CandidateFilters(school_level="普通"), attrs)) == ["hard_filter"]


def test_index_row_maps_to_candidate_attrs():
    attrs = CandidateAttrs.from_index_row({
        "total_years": 7.5, "highest_degree": "MASTER", "age": 30, "qs_rank": 120,
        "location_terms": ["深圳"], "preferred_locations": ["杭州"],
        "company_terms": ["腾讯"], "title_terms": ["后端工程师"], "school_terms": ["深圳大学"],
        "career_directions": ["BACKEND"], "business_directions": ["FINANCE"],
        "keyword_index_text": "java 后端",
    })
    assert attrs.total_years == 7.5
    assert attrs.locations == ("深圳",)
    assert attrs.preferred_locations == ("杭州",)
    assert set(attrs.direction_values) == {"BACKEND", "FINANCE"}
    assert audit_hard_filters(CandidateFilters(career_directions=("BACKEND",)), attrs) == []


# --- 汇总 ---


def test_summarize_plans_reports_fallback_rate_and_latency():
    summary = summarize_plans([
        {"degraded": None, "source": "llm", "accepted_fields": ("min_years",), "elapsed_ms": 400.0},
        {"degraded": "timeout", "source": "rule", "accepted_fields": (), "elapsed_ms": 2600.0},
        {"degraded": None, "source": "mixed", "accepted_fields": ("min_years", "locations"), "elapsed_ms": 800.0},
    ])
    assert summary["queries"] == 3
    assert summary["degraded_total"] == 1
    assert summary["degraded_rate"] == 0.3333
    assert summary["degraded_kinds"] == {"timeout": 1}
    assert summary["source_distribution"] == {"llm": 1, "mixed": 1, "rule": 1}
    assert summary["accepted_field_distribution"] == {"min_years": 2, "locations": 1}
    assert summary["parse_ms_max"] == 2600.0
    assert summary["parse_ms_p50"] == 800.0


def test_summarize_violations_and_percentile():
    assert summarize_violations([]) == {}
    assert percentile([], 0.5) == 0.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 20.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 40.0


def test_condition_selectivity_explains_a_structurally_empty_result():
    """空结果归因：条件组合结构性无解时上界为 0，且能指出是哪条条件卡住的。"""
    attrs = {
        "a": [CandidateAttrs(total_years=8.0, locations=("深圳",), direction_values=("BACKEND",))],
        "b": [CandidateAttrs(total_years=3.0, locations=("北京",), direction_values=("FRONTEND",))],
    }
    ok = condition_selectivity(CandidateFilters(min_years=5.0, locations=("深圳",)), attrs)
    assert ok["satisfying_all"] == 1
    assert ok["per_field"] == {"min_years": 1, "locations": 1}

    # 年限与城市分别各有一人满足，但没有一个人同时满足 → 结构性无解。
    impossible = condition_selectivity(
        CandidateFilters(min_years=5.0, locations=("北京",)), attrs)
    assert impossible["satisfying_all"] == 0
    assert impossible["per_field"] == {"min_years": 1, "locations": 1}


def test_condition_selectivity_ignores_empty_filters():
    attrs = {"a": [CandidateAttrs(total_years=1.0)]}
    result = condition_selectivity(CandidateFilters(), attrs)
    assert result == {"candidates": 1, "satisfying_all": 1, "per_field": {}}


def test_candidate_is_satisfied_when_any_revision_row_matches():
    """同一候选人多个当前修订各有一行：只要有一行满足，候选人就不算违反。"""
    filters = CandidateFilters(locations=("深圳",))
    rows = [CandidateAttrs(locations=("北京",)), CandidateAttrs(locations=("深圳",))]
    assert audit_candidate(filters, rows) == []
    assert kinds(audit_candidate(filters, [CandidateAttrs(locations=("北京",))])) == ["hard_filter"]
    assert audit_candidate(filters, []) == []


def test_satisfies_filters_requires_positive_evidence():
    """选择性重算要求正面证据：属性缺失等同于不满足（索引侧 NULL/空列不会匹配）。"""
    filters = CandidateFilters(title="全栈工程师", locations=("深圳",))
    assert satisfies_filters(filters, CandidateAttrs(title_text="全栈工程师", locations=("深圳",)))
    # title 缺失：不可召，算不满足（若按 audit_hard_filters 会被算成「未知不违反」）。
    assert not satisfies_filters(filters, CandidateAttrs(locations=("深圳",)))
    assert not satisfies_filters(CandidateFilters(min_years=5.0), CandidateAttrs())
    assert satisfies_filters(CandidateFilters(min_years=5.0), CandidateAttrs(total_years=6.0))
