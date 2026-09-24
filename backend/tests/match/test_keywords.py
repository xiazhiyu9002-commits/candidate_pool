"""匹配关键词构造单测（方案 §2）。

覆盖：技术/业务两类来源、token 预算裁剪、被排除的 label、去重与子串包含。
"""
from kerui_recruit.match.keywords import (
    BIZ_TOKEN_BUDGET,
    TECH_TOKEN_BUDGET,
    build_candidate_biz_terms,
    build_candidate_query_text,
    build_candidate_tech_terms,
    build_jd_biz_terms,
    build_jd_query_text,
    build_jd_tech_terms,
)
from kerui_recruit.search.lexicon import tokenize_lexical_text


def _jd() -> dict:
    return {
        "required_skills": ["Java", "Spring Boot"],
        "requirements": [
            {"kind": "MUST", "label": "技能", "value": "Java"},          # 与 required_skills 重复
            {"kind": "PLUS", "label": "技能", "value": "Kubernetes"},     # 技术类
            {"kind": "MUST", "label": "行业", "value": "保险"},           # 业务类
            {"kind": "PLUS", "label": "领域知识", "value": "交易结算"},     # 业务类
            {"kind": "MUST", "label": "学历", "value": "本科及以上"},      # 排除
            {"kind": "MUST", "label": "软技能", "value": "沟通协作能力"},  # 排除
            {"kind": "PLUS", "label": "证书", "value": "Avaloq 认证"},    # 排除
            {"kind": "PLUS", "label": "能力", "value": "机器学习理论基础"},  # 模糊 label，排除
            {"kind": "MUST", "label": "年限", "value": "5年以上经验"},     # 排除
        ],
        "core_duties": ["负责保险核心系统的高并发改造与稳定性建设"],
        "business_directions": ["INSURANCE"],
        "plus_industry": ["金融"],
        "plus_project_types": ["核心系统"],
    }


def test_jd_tech_terms_use_skills_and_skill_label_only() -> None:
    terms = build_jd_tech_terms(_jd())
    assert terms[:2] == ["Java", "Spring Boot"]
    assert "Kubernetes" in terms
    # 重复的 MUST 技能被去重；被排除的 label 一律不出现。
    assert terms.count("Java") == 1
    for dropped in ("本科及以上", "沟通协作能力", "Avaloq 认证", "机器学习理论基础", "5年以上经验"):
        assert dropped not in terms


def test_jd_biz_terms_use_structured_fields_and_biz_labels() -> None:
    terms = build_jd_biz_terms(_jd())
    assert "保险" in terms and "金融" in terms
    assert "交易结算" in terms and "核心系统" in terms
    # 职责长句属于自由文本，不进关键词。
    assert all("负责保险核心系统" not in term for term in terms)


def test_jd_query_text_excludes_duties_and_dropped_labels() -> None:
    query = build_jd_query_text(_jd())
    assert "Java" in query and "保险" in query
    assert "负责保险核心系统" not in query
    assert "本科及以上" not in query and "沟通协作能力" not in query


def test_contained_short_terms_are_dropped() -> None:
    """「Python」被「Python or Java」覆盖时只保留长词，避免重复占预算。"""
    parsed = {"required_skills": ["Python or Java", "Python"]}
    assert build_jd_tech_terms(parsed) == ["Python or Java"]


def test_descriptive_requirement_does_not_swallow_skill_words() -> None:
    """句子式的技能要求没有吞并资格——否则它会把 required_skills 的短词整片删掉。

    实测「数仓技术TL」：``精通Hadoop生态（Hive/Spark/Flink/Kafka等）`` 含了这些 token，
    曾把 5 个短词吞掉，tech 词只剩 2 条，查询退化成一句长噪声（方案 §2 明确要排除的形态）。
    """
    parsed = {
        "required_skills": ["Hadoop", "Hive", "Spark", "Flink", "Kafka"],
        "requirements": [
            {"kind": "MUST", "label": "技能",
             "value": "精通Hadoop生态（Hive/Spark/Flink/Kafka等）"},
        ],
    }
    terms = build_jd_tech_terms(parsed)
    assert {"Hadoop", "Hive", "Spark", "Flink", "Kafka"} <= set(terms)


def test_token_budget_truncates_tail() -> None:
    parsed = {"required_skills": [f"技能词{i}" for i in range(200)]}
    terms = build_jd_tech_terms(parsed)
    total = len(tokenize_lexical_text(" ".join(terms)))
    assert terms and total <= TECH_TOKEN_BUDGET
    assert len(terms) < 200


def test_candidate_terms_use_skills_experience_and_business_labels() -> None:
    parsed = {
        "skills": ["Python", "Java"],
        "experiences": [{"tech_stack": ["Spark", "Flink"]}],
        "business_directions": ["BANKING"],
        "projects": [{"business_scene": "面向保险核心承保与理赔的高并发交易系统建设"}],
    }
    tech = build_candidate_tech_terms(parsed)
    assert {"Python", "Java", "Spark", "Flink"} <= set(tech)
    biz = build_candidate_biz_terms(parsed)
    # 业务词只取业务方向标签的中文名，项目业务场景自由文本不进关键词。
    assert biz == ["银行"]
    assert all("高并发交易系统" not in term for term in biz)


def test_invalid_or_other_business_directions_are_dropped() -> None:
    parsed = {"business_directions": ["FINANCE", "OTHER", "INSURANCE"]}
    assert build_candidate_biz_terms(parsed) == ["保险"]


def test_candidate_query_text_excludes_screening_fields() -> None:
    parsed = {
        "name": "张三",
        "skills": ["Python"],
        "current_company": "腾讯科技",
        "location": "上海",
        "total_years": 8,
        "qs_rank": 50,
        "ai_profile_summary": "资深后端工程师",
        "business_directions": ["INSURANCE"],
    }
    query = build_candidate_query_text(parsed)
    assert "Python" in query
    for filtered in ("张三", "腾讯科技", "上海", "50", "资深后端工程师"):
        assert filtered not in query


def test_biz_terms_respect_token_budget() -> None:
    parsed = {"plus_industry": [f"行业{i}" for i in range(200)]}
    terms = build_jd_biz_terms(parsed)
    assert len(tokenize_lexical_text(" ".join(terms))) <= BIZ_TOKEN_BUDGET


def test_empty_parsed_data_yields_empty_query() -> None:
    assert build_jd_query_text({}) == ""
    assert build_candidate_query_text({}) == ""
