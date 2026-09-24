from kerui_recruit.search.documents import (
    PARENT_SKILL_LIMIT,
    _CHILD_PREFIX_MAX,
    build_candidate_document,
    build_child_documents,
    build_candidate_document_b,
    build_candidate_document_c,
    build_child_documents_c,
)
from kerui_recruit.search.sync import build_jd_documents_b


def test_document_includes_current_company_and_title_in_terms() -> None:
    doc = build_candidate_document({
        "name": "张三",
        "current_company": "腾讯科技",
        "current_title": "技术专家",
        "experiences": [
            {"company": "阿里巴巴", "title": "高级工程师"},
            {"company": "京东", "title": "工程师"},
        ],
        "skills": ["Go", "Java"],
    })

    assert "腾讯科技" in doc["company_terms"]
    assert "技术专家" in doc["title_terms"]
    assert "阿里巴巴" in doc["company_terms"]
    assert "京东" in doc["company_terms"]
    assert "腾讯科技" in doc["keyword_text"]


def test_document_deduplicates_current_company_already_in_experiences() -> None:
    doc = build_candidate_document({
        "name": "张三",
        "current_company": "腾讯科技",
        "current_title": "工程师",
        "experiences": [{"company": "腾讯科技", "title": "工程师"}],
    })

    assert doc["company_terms"].count("腾讯科技") == 1
    assert doc["title_terms"].count("工程师") == 1


def test_document_emits_multi_value_directions() -> None:
    doc = build_candidate_document({
        "name": "张三",
        "career_directions": ["BACKEND", "DATA"],
        "career_specializations": ["BACKEND_SERVICE", "DATA_ANALYSIS"],
        "business_directions": ["INSURANCE", "MARKETING"],
    })

    assert doc["career_directions"] == ["BACKEND", "DATA"]
    assert doc["career_specializations"] == ["BACKEND_SERVICE", "DATA_ANALYSIS"]
    assert doc["business_directions"] == ["INSURANCE", "MARKETING"]
    # 旧列镜像细分代码，保证未升级的读侧仍有值
    assert doc["specializations"] == ["BACKEND_SERVICE", "DATA_ANALYSIS"]


def test_document_deduplicates_multi_value_directions() -> None:
    doc = build_candidate_document({
        "name": "张三",
        "career_directions": ["BACKEND", "BACKEND"],
        "business_directions": ["INSURANCE", "INSURANCE"],
    })
    assert doc["career_directions"] == ["BACKEND"]
    assert doc["business_directions"] == ["INSURANCE"]


def test_document_falls_back_to_legacy_direction_and_assessment() -> None:
    """存量数据只有单值 direction + 旧专长时，仍应产出现代方向字段供筛选。"""
    doc = build_candidate_document({
        "name": "张三",
        "direction": "BACKEND",
        "direction_assessment": {"primary": "BACKEND", "specializations": ["FULL_STACK"]},
    })

    assert doc["career_directions"] == ["BACKEND"]
    assert doc["career_specializations"] == ["FULL_STACK"]


def test_document_without_direction_data_emits_empty_lists() -> None:
    doc = build_candidate_document({"name": "张三", "skills": ["Java"]})
    assert doc["career_directions"] == []
    assert doc["career_specializations"] == []
    assert doc["business_directions"] == []


def test_candidate_document_separates_three_surfaces() -> None:
    doc = build_candidate_document({
        "name": "张三",
        "skills": ["JS", "k8s"],
        "ai_profile_summary": "负责交易系统开发",
    })
    assert "javascript" in doc["keyword_index_text"].split()
    assert "js" in doc["keyword_index_text"].split()
    assert "kubernetes" in doc["keyword_index_text"].split()
    assert "张三" in doc["keyword_text"]
    assert doc["vector_text"].count("JavaScript") == 1
    assert " JS " not in f" {doc['vector_text']} "


def test_java_and_javascript_remain_distinct_tokens() -> None:
    doc = build_candidate_document({"skills": ["JavaScript"]})
    assert "javascript" in doc["keyword_index_text"].split()
    assert "java" not in doc["keyword_index_text"].split()


def test_school_aliases_expand_only_the_lexical_surface() -> None:
    doc = build_candidate_document(
        {"educations": [{"school": "北京大学"}]},
        school_alias_groups={"北京大学": ("北大", "PKU")},
    )
    assert {"北京大学", "北大", "pku"} <= set(doc["keyword_index_text"].split())
    assert "北大" not in doc["vector_text"]


def test_degree_aliases_share_the_normalized_degree_filter_vocabulary() -> None:
    doc = build_candidate_document({"highest_degree": "BACHELOR"})
    assert {"bachelor", "本科", "学士"} <= set(doc["keyword_index_text"].split())


def test_location_terms_only_carry_current_location() -> None:
    """现居筛选只看现居字段：意向地、经历地不得混入 location_terms。"""
    doc = build_candidate_document({
        "location": "杭州",
        "preferred_location": "上海",
        "preferred_locations": ["北京"],
        "experiences": [{"company": "腾讯", "title": "工程师", "location": "深圳"}],
    })
    assert doc["location_terms"] == ["杭州"]


def test_location_terms_are_normalized_for_precise_filtering() -> None:
    """非规范城市写法必须归一化，否则 array_has_any 永远召不回这些行。"""
    doc = build_candidate_document({"location": "广东省深圳市"})
    assert doc["location_terms"] == ["深圳"]
    assert build_candidate_document({"location": "成都/杭州"})["location_terms"] == ["成都", "杭州"]
    # 认不出的写法原样保留，不能把值弄空。
    assert build_candidate_document({"location": "远程"})["location_terms"] == ["远程"]


def test_body_index_text_includes_experience_and_project_summaries() -> None:
    """可选「检索正文」应包含工作职责与项目描述，但不含 tech_stack。"""
    doc = build_candidate_document({
        "name": "张三",
        "experiences": [{"company": "腾讯", "title": "工程师", "summary": "负责支付系统的设计与实现"}],
        "projects": [{"tech_stack": "Python, Flink", "summary": "搭建风控平台处理流式数据"}],
    })
    body = doc["body_index_text"]
    assert "支付" in body
    assert "风控" in body
    # tech_stack 不应进入正文检索字段（正文范围仅职责+项目描述）。
    assert "python" not in body.split()


def test_body_index_text_does_not_pollute_keyword_or_terms() -> None:
    """正文只进入 body_index_text，不污染 keyword_index_text 与字段 term。"""
    doc = build_candidate_document({
        "name": "张三",
        "skills": ["Java"],
        "experiences": [{"company": "腾讯", "title": "工程师", "summary": "负责高并发系统"}],
    })
    assert "高并发" in doc["body_index_text"] or "并发" in doc["body_index_text"]
    assert "并发" not in doc["keyword_index_text"]
    assert doc["company_terms"] == ["腾讯"]
    assert doc["title_terms"] == ["工程师"]


def test_build_child_documents_splits_experiences_and_projects() -> None:
    children = build_child_documents({
        "experiences": [
            {"company": "腾讯", "title": "工程师", "summary": "负责支付系统"},
            {"company": "阿里", "title": "架构师", "summary": "负责高并发"},
        ],
        "projects": [{"tech_stack": "Python, Flink", "summary": "搭建风控平台"}],
    })
    assert len(children) == 3
    assert children[0]["kind"] == "experience"
    assert "支付" in children[0]["keyword_index_text"]
    assert children[2]["kind"] == "project"
    assert "flink" in children[2]["keyword_index_text"]
    assert "python" in children[2]["keyword_index_text"]


def test_build_child_documents_skips_empty_entries() -> None:
    children = build_child_documents({
        "experiences": [{"company": "", "title": "", "summary": ""}],
        "projects": [],
    })
    assert children == []


def test_build_child_documents_includes_project_name_and_business_scene() -> None:
    children = build_child_documents({
        "projects": [{"name": "智能推荐系统", "business_scene": "电商", "tech_stack": "Python", "summary": "召回排序"}],
    })
    project = next(c for c in children if c["kind"] == "project")
    assert "智能推荐系统" in project["vector_text"]
    assert "电商" in project["vector_text"]
    assert "电商" in project["keyword_index_text"]


def test_v9_parent_vector_keeps_semantic_surface_only() -> None:
    """v9 父向量：保留职业定位 / 业务方向 / 最近岗位名 / 技术概览，排除弱语义字段。"""
    doc = build_candidate_document({
        "name": "张三",
        "phone": "13800138000",
        "skills": ["Java"],
        "ai_profile_summary": "负责交易系统开发",
        "educations": [{"school": "北京大学", "degree": "MASTER", "major": "计算机"}],
        "location": "上海",
        "total_years": 5,
        "current_company": "腾讯科技",
        "current_title": "技术专家",
        "business_directions": ["PAYMENT"],
    })
    vector_text = doc["vector_text"]
    for excluded in ("北京大学", "上海", "5年", "张三", "13800138000", "腾讯科技", "MASTER"):
        assert excluded not in vector_text
    assert "交易系统" in vector_text
    assert "技术专家" in vector_text
    assert "Java" in vector_text
    assert "支付与清结算" in vector_text
    # 精确筛选仍走词法面与结构化列，不受父向量口径影响。
    assert "北京大学" in doc["keyword_text"]
    assert "上海" in doc["keyword_text"]


def test_v9_parent_skills_are_capped() -> None:
    doc = build_candidate_document({"skills": [f"Skill{i}" for i in range(PARENT_SKILL_LIMIT + 10)]})
    tokens = doc["vector_text"].split()
    assert len([token for token in tokens if token.startswith("Skill")]) == PARENT_SKILL_LIMIT
    # keyword 面不做截断，精确筛选与 FTS 仍能看到全部技能。
    assert f"Skill{PARENT_SKILL_LIMIT + 9}" in doc["keyword_text"]


def test_v9_child_prefix_is_bounded() -> None:
    """子片段的职业定位前缀必须压到 _CHILD_PREFIX_MAX 以内，避免淹没本段经历。"""
    long_profile = "很长的职业定位" * 20
    children = build_child_documents({
        "ai_profile_compact": long_profile,
        "experiences": [{"company": "某公司", "title": "工程师", "summary": "负责支付系统"}],
    })
    experience = next(c for c in children if c["kind"] == "experience")
    assert experience["vector_text"].startswith(long_profile[:_CHILD_PREFIX_MAX])
    assert long_profile[:_CHILD_PREFIX_MAX + 1] not in experience["vector_text"]
    assert "负责支付系统" in experience["vector_text"]


def test_v9_experience_vector_includes_tech_stack() -> None:
    children = build_child_documents({
        "experiences": [{"company": "某公司", "title": "工程师", "tech_stack": ["Kafka", "Redis"], "summary": "支付"}],
    })
    experience = next(c for c in children if c["kind"] == "experience")
    assert "Kafka" in experience["vector_text"]
    assert "Redis" in experience["vector_text"]
    assert "kafka" in experience["keyword_index_text"]


def test_variant_b_vector_excludes_school_location_years_and_name() -> None:
    doc = build_candidate_document_b({
        "name": "张三",
        "phone": "13800138000",
        "skills": ["Java"],
        "ai_profile_summary": "负责交易系统开发",
        "educations": [{"school": "北京大学", "degree": "MASTER", "major": "计算机"}],
        "location": "上海",
        "total_years": 5,
        "current_company": "腾讯科技",
        "current_title": "技术专家",
    })
    # 弱语义/敏感字段不进入向量
    assert "北京大学" not in doc["vector_text"]
    assert "上海" not in doc["vector_text"]
    assert "5年" not in doc["vector_text"]
    assert "张三" not in doc["vector_text"]
    assert "13800138000" not in doc["vector_text"]
    # 语义面保留画像 + 最近公司/职位 + 技能
    assert "交易系统" in doc["vector_text"]
    assert "腾讯科技" in doc["vector_text"]
    assert "技术专家" in doc["vector_text"]
    assert "Java" in doc["vector_text"]
    # 学校/城市/年限仍可用于精确过滤（词法面保留）
    assert "北京大学" in doc["keyword_text"]
    assert "上海" in doc["keyword_text"]


def test_variant_b_child_documents_keep_work_and_project_evidence() -> None:
    doc = build_candidate_document_b({
        "name": "张三",
        "skills": ["Java"],
        "experiences": [{"company": "腾讯", "title": "工程师", "summary": "负责支付系统"}],
        "projects": [{"tech_stack": "Python/Flink", "summary": "搭建风控平台"}],
    })
    children = build_child_documents({
        "experiences": [{"company": "腾讯", "title": "工程师", "summary": "负责支付系统"}],
        "projects": [{"tech_stack": "Python/Flink", "summary": "搭建风控平台"}],
    })
    # 工作/项目独立证据不丢
    assert len(children) == 2
    assert "支付" in children[0]["keyword_index_text"]
    assert "flink" in children[1]["keyword_index_text"]
    assert doc["vector_text"]  # 父向量仍非空


def test_variant_c_child_documents_include_project_name_and_business() -> None:
    children = build_child_documents_c({
        "projects": [{"name": "智能客服", "tech_stack": "Python", "summary": "RAG 问答", "business": "金融"}],
    })
    assert len(children) == 1
    assert children[0]["kind"] == "project"
    # 项目名称与业务场景合入项目段（语义面）
    assert "智能客服" in children[0]["vector_text"]
    assert "金融" in children[0]["vector_text"]
    assert "RAG" in children[0]["vector_text"]


def test_variant_c_short_or_empty_project_segment_is_skipped() -> None:
    children = build_child_documents_c({
        "projects": [{"name": "", "tech_stack": "", "summary": "", "business": ""}],
    })
    assert children == []


class _FakeJd:
    company = "汇丰"
    title = "大模型算法"


class _FakeRev:
    id = "jd1"
    min_years = None
    highest_degree = None
    location = None
    source_text = "JD 原文"
    parsed_data = {
        "required_skills": ["Python", "Transformer"],
        "plus_skills": ["RAG"],
        "core_duties": ["负责 RAG 与 LLM 模型构建"],
        "summary": "硕士及以上",
        "candidate_profile": "目标候选人画像",
    }


def test_variant_b_jd_drops_single_skill_child_vectors() -> None:
    docs = build_jd_documents_b(_FakeJd(), _FakeRev())
    chunk_types = [d["chunk_type"] for d in docs]
    assert chunk_types.count("parent") == 1
    # 职责子 chunk 保留
    assert any(d["vector_text"] == "负责 RAG 与 LLM 模型构建" for d in docs)
    # 单技能短子向量被去掉
    assert not any(d["chunk_type"] == "child" and d["vector_text"] in ("Python", "Transformer") for d in docs)
    # 技能仍在词法面（父 chunk keyword_text 含技能），供 FTS 与硬规则
    parent = next(d for d in docs if d["chunk_type"] == "parent")
    assert "Python" in parent["keyword_text"]
    assert "Transformer" in parent["keyword_text"]
