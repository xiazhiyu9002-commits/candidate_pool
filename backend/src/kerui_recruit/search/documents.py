"""Centralized construction of the candidate search document.

The keyword text and vector text are intentionally different:

- ``keyword_text`` carries the full lexical surface (name, all schools/degrees,
  city, all companies/titles, skills, AI profile, readable age/years/QS). It is
  the single target for FTS recall and exclusion evidence. It must NOT contain
  the phone number, job responsibility body or project body.
- ``vector_text`` carries only the semantic surface (AI profile, education,
  city/years, most-recent company/title and skills). It is embedded into the
  vector and reused for semantic reranking.

Field term lists power precise field filters and must never be polluted by
terms appearing in the AI profile or another field.
"""
from __future__ import annotations

from kerui_recruit.direction.policy import extract_multi_directions
from kerui_recruit.search.lexicon import (
    expand_degree_tokens,
    expand_document_tokens,
    expand_school_tokens,
    normalize_skill,
    tokenize_lexical_text,
)


def _as_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    return [str(value)]


def _flat(value: object) -> str:
    """把 str / list[str] 统一压成空格分隔文本，供子 chunk 文本拼接（列表不会带 Python repr）。"""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v) for v in value if str(v).strip())
    return str(value or "").strip()


def _uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _text(*parts: object) -> str:
    # 画像可能分点（换行分隔）：向量/关键词文本统一压成一段，避免换行影响检索与向量化。
    return " ".join(
        str(p).replace("\n", " ").replace("\r", " ")
        for p in parts if p is not None and str(p).strip()
    )


_CN_REGIONS = {"中国", "国内", "大陆", "中国大陆"}
_DEGREE_RANK = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}


def _school_region(data: dict) -> str:
    """按最高学历教育经历的属地判定 domestic / overseas（无法判定归为 domestic）。

    与前端「学校等级」列的海归判定一致：最高学历的 school_tags 含「海外」，
    或 country_region 非空且非中国，即判定为 overseas。
    """
    educations = data.get("educations") or []
    highest: dict | None = None
    highest_rank = -1
    for edu in educations:
        if not isinstance(edu, dict):
            continue
        rank = _DEGREE_RANK.get(str(edu.get("degree") or ""), 0)
        if rank > highest_rank:
            highest = edu
            highest_rank = rank
    if highest is None:
        return "domestic"
    tags = [str(t) for t in (highest.get("school_tags") or [])]
    region = str(highest.get("country_region") or "").strip()
    if "海外" in tags or (region and region not in _CN_REGIONS):
        return "overseas"
    return "domestic"


def _profile_text(data: dict) -> str:
    """双形态画像的「整体段落」作为父向量主文本，回退旧摘要/原始 summary。"""
    return str(
        data.get("ai_profile_narrative")
        or data.get("ai_profile_summary")
        or data.get("summary")
        or ""
    )


def build_candidate_document(
    data: dict,
    *,
    display_name: str | None = None,
    school_alias_groups: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """Build the search document fields from a normalized resume dict.

    ``data`` is the JSON-serializable ``parsed_data`` of the current revision.
    ``display_name`` overrides ``data["name"]`` when the human-edited candidate
    name is the authoritative value.
    """
    name = display_name or data.get("name")
    skills = _uniq(_as_list(data.get("skills")))
    canonical_skills = _uniq([normalize_skill(skill) for skill in skills])

    educations = data.get("educations") or []
    school_terms: list[str] = []
    education_text: list[str] = []
    school_tags: list[str] = []
    for edu in educations:
        if not isinstance(edu, dict):
            continue
        school = edu.get("school")
        if school:
            school_terms.append(str(school))
            education_text.append(str(school))
        for key in ("degree", "major"):
            value = edu.get(key)
            if value:
                education_text.append(str(value))
        school_tags.extend(str(t) for t in _as_list(edu.get("school_tags")) if t)

    # Legacy single school field is a compatibility fallback.
    legacy_school = data.get("school")
    if legacy_school and not school_terms:
        school_terms.append(str(legacy_school))
        education_text.append(str(legacy_school))

    experiences = data.get("experiences") or []
    company_terms: list[str] = []
    title_terms: list[str] = []
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        if exp.get("company"):
            company_terms.append(str(exp["company"]))
        if exp.get("title"):
            title_terms.append(str(exp["title"]))
    # 当前公司/职位作为有效检索值，人工编辑后必须能被精确筛选命中。
    if data.get("current_company"):
        company_terms.insert(0, str(data["current_company"]))
    if data.get("current_title"):
        title_terms.insert(0, str(data["current_title"]))

    # 现居城市筛选只看现居字段（合同冻结地点语义）；意向地点由 preferred_locations 单独承载，
    # 工作经历中的地点不得混入现居过滤，避免「现居上海」误命中「意向上海/曾在上海工作」的人。
    location_terms: list[str] = []
    if data.get("location"):
        location_terms.append(str(data["location"]))

    school_level = data.get("school_level")
    if school_level:
        school_tags.append(str(school_level))

    age = data.get("age")
    age_int: int | None = None
    if age is not None:
        try:
            age_int = int(age)
        except (TypeError, ValueError):
            age_int = None

    total_years = data.get("total_years")
    qs_rank = data.get("qs_rank")

    # Recent company/title for the vector surface.
    recent_company = data.get("current_company") or (company_terms[0] if company_terms else None)
    recent_title = data.get("current_title") or (title_terms[0] if title_terms else None)

    keyword_parts = [
        name,
        _profile_text(data),
        *education_text,
        data.get("location"),
        data.get("highest_degree"),
        *company_terms,
        *title_terms,
        *skills,
        _readable_age(age_int),
        _readable_years(total_years),
        f"QS{qs_rank}" if qs_rank else "",
    ]
    keyword_text = _text(*keyword_parts)

    vector_parts = [
        _profile_text(data),
        *education_text,
        data.get("location"),
        _readable_years(total_years),
        recent_company,
        recent_title,
        *canonical_skills,
    ]
    vector_text = _text(*vector_parts)

    keyword_index_text = _build_keyword_index_text(
        data,
        name=name,
        skills=skills,
        educations=educations,
        school_alias_groups=school_alias_groups,
        company_terms=company_terms,
        title_terms=title_terms,
        age_int=age_int,
        total_years=total_years,
        qs_rank=qs_rank,
    )

    body_index_text = _build_body_index_text(data)

    # 多值方向：顶层缺省时回退 direction_assessment / 单值 direction（存量兼容）。
    career_directions, career_specializations, business_directions = (
        list(values) for values in extract_multi_directions(data)
    )

    return {
        "keyword_index_text": keyword_index_text,
        "keyword_text": keyword_text,
        "vector_text": vector_text,
        "body_index_text": body_index_text,
        "name_terms": _uniq([str(name)] if name else []),
        "school_terms": _uniq(school_terms),
        "company_terms": _uniq(company_terms),
        "title_terms": _uniq(title_terms),
        "location_terms": _uniq(location_terms),
        "skills": skills,
        "age": age_int,
        "school_tags": _uniq(school_tags),
        "direction": data.get("direction"),
        "school_region": _school_region(data),
        # 旧列：镜像细分代码，供未升级的读侧继续取到值；新读侧一律用 career_specializations。
        "specializations": career_specializations,
        "career_directions": career_directions,
        "career_specializations": career_specializations,
        "business_directions": business_directions,
    }


def _readable_age(age: int | None) -> str:
    if age is None:
        return ""
    return f"{age}岁"


def _readable_years(total_years: object) -> str:
    if total_years is None:
        return ""
    try:
        return f"{float(total_years):g}年"
    except (TypeError, ValueError):
        return ""


def _build_keyword_index_text(
    data: dict,
    *,
    name,
    skills: list[str],
    educations,
    school_alias_groups: dict[str, tuple[str, ...]] | None,
    company_terms: list[str],
    title_terms: list[str],
    age_int: int | None,
    total_years,
    qs_rank,
) -> str:
    """空格分隔、casefold 的规范词与别名，仅供 FTS 词级检索。"""
    tokens: list[str] = []
    tokens.extend(tokenize_lexical_text(str(name) if name else ""))
    tokens.extend(tokenize_lexical_text(
        _profile_text(data)
    ))
    for edu in educations:
        if not isinstance(edu, dict):
            continue
        school = edu.get("school")
        if school:
            tokens.extend(expand_school_tokens(str(school), school_alias_groups))
        for key in ("degree", "major"):
            value = edu.get(key)
            if not value:
                continue
            if key == "degree":
                tokens.extend(expand_degree_tokens(str(value)))
            else:
                tokens.extend(tokenize_lexical_text(str(value)))
    tokens.extend(tokenize_lexical_text(str(data.get("location") or "")))
    if data.get("highest_degree"):
        tokens.extend(expand_degree_tokens(str(data["highest_degree"])))
    for company in company_terms:
        tokens.extend(tokenize_lexical_text(company))
    for title in title_terms:
        tokens.extend(tokenize_lexical_text(title))
    tokens.extend(expand_document_tokens(skills))
    tokens.extend(tokenize_lexical_text(_readable_age(age_int)))
    tokens.extend(tokenize_lexical_text(_readable_years(total_years)))
    if qs_rank:
        tokens.extend(tokenize_lexical_text(f"QS{qs_rank}"))
    return " ".join(_uniq(tokens))


def _build_body_index_text(data: dict) -> str:
    """把工作职责正文与项目描述正文规范化为词级检索文本。

    仅用于可选「检索正文」的 FTS 召回，不进入 keyword_index_text，也不进入任何
    字段 term 列表，因此不会污染精确筛选。只做技术标记保护 + 中文分词 + casefold，
    不做技能别名展开（正文是自由文本，非技能列表）。
    """
    parts: list[str] = []
    for exp in data.get("experiences") or []:
        if isinstance(exp, dict) and exp.get("summary"):
            parts.append(str(exp["summary"]))
    for proj in data.get("projects") or []:
        if isinstance(proj, dict) and proj.get("summary"):
            parts.append(str(proj["summary"]))
    tokens: list[str] = []
    for part in parts:
        tokens.extend(tokenize_lexical_text(part))
    return " ".join(_uniq(tokens))


def _profile_point_items(data: dict) -> list[tuple[str, list[str]]]:
    """返回 (分点文本, 证据路径) 列表：优先结构化分点字段，否则按整体画像换行拆点（无证据路径）。"""
    explicit = data.get("ai_profile_points")
    if explicit:
        items: list[tuple[str, list[str]]] = []
        for p in explicit:
            if isinstance(p, dict):
                text = str(p.get("text") or "").strip()
                paths = [str(x) for x in (p.get("evidence_paths") or []) if x]
            else:
                text = str(p).strip()
                paths = []
            if text:
                items.append((text, paths))
        if items:
            return items
    summary = _profile_text(data).strip()
    if not summary:
        return []
    return [(line.strip(), []) for line in summary.splitlines() if line.strip()]


def _profile_points(data: dict) -> list[str]:
    return [text for text, _ in _profile_point_items(data)]


def _compact_profile(data: dict) -> str:
    """画像浓缩前缀：显式 compact 字段，或整体画像首句截断（足够短，不淹没子片段）。"""
    compact = str(data.get("ai_profile_compact") or "").strip()
    if compact:
        return compact
    points = _profile_points(data)
    return points[0][:60] if points else ""


def build_child_documents(data: dict) -> list[dict]:
    """为画像分点、工作经历与项目经历生成「子 chunk」文档。

    每个子 chunk 独立向量化，其 vector_text 为「简短画像浓缩前缀 + 本片段内容」，
    避免前缀淹没具体项目/工作经历；同时记录 parent_id、片段类型与序号/证据路径。
    子 chunk 不写入硬条件字段（年限/学历/地点等），硬过滤仍由父 chunk 承担。
    """
    compact = _compact_profile(data)
    prefix = f"{compact} " if compact else ""
    children: list[dict] = []

    # 画像分点作为独立子切片（证据路径取自结构化分点，缺省回退位置序号）。
    for i, (point, paths) in enumerate(_profile_point_items(data)):
        children.append({
            "kind": "profile_point",
            "sequence": i,
            "evidence_path": paths or [f"ai_profile_points[{i}]"],
            "vector_text": _text(prefix + point),
            "keyword_index_text": " ".join(_uniq(tokenize_lexical_text(point))),
        })

    for i, exp in enumerate(data.get("experiences") or []):
        if not isinstance(exp, dict):
            continue
        parts = [
            str(exp.get("company") or ""),
            str(exp.get("title") or ""),
            str(exp.get("summary") or ""),
        ]
        if not any(p.strip() for p in parts):
            continue
        tokens: list[str] = []
        for part in parts:
            tokens.extend(tokenize_lexical_text(part))
        children.append({
            "kind": "experience",
            "sequence": i,
            "evidence_path": f"experiences[{i}]",
            "vector_text": _text(prefix + " ".join(p for p in parts if p.strip())),
            "keyword_index_text": " ".join(_uniq(tokens)),
        })

    for i, proj in enumerate(data.get("projects") or []):
        if not isinstance(proj, dict):
            continue
        parts = [
            _flat(proj.get("name")),
            _flat(proj.get("business_scene")),
            _flat(proj.get("tech_stack")),
            _flat(proj.get("summary")),
        ]
        if not any(p.strip() for p in parts):
            continue
        tokens = []
        for part in parts:
            tokens.extend(tokenize_lexical_text(part))
        children.append({
            "kind": "project",
            "sequence": i,
            "evidence_path": f"projects[{i}]",
            "vector_text": _text(prefix + " ".join(p for p in parts if p.strip())),
            "keyword_index_text": " ".join(_uniq(tokens)),
        })
    return children


def build_candidate_document_b(
    data: dict,
    *,
    display_name: str | None = None,
    school_alias_groups: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """变体 B：父向量去掉学校/城市/年限/姓名，仅保留画像 + 最近公司/职位 + 技能。

    学校、城市、年限、姓名仍保留在 keyword_text/keyword_index_text，供精确过滤与
    词法召回；工作/项目仍由 build_child_documents 生成独立子向量。本变体不新增
    任何生成模型调用，仅改变父 chunk 的 vector_text 构成，供隔离索引消融对比。
    """
    doc = build_candidate_document(
        data, display_name=display_name, school_alias_groups=school_alias_groups
    )

    skills = _uniq(_as_list(data.get("skills")))
    canonical_skills = _uniq([normalize_skill(skill) for skill in skills])

    experiences = data.get("experiences") or []
    company_terms: list[str] = []
    title_terms: list[str] = []
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        if exp.get("company"):
            company_terms.append(str(exp["company"]))
        if exp.get("title"):
            title_terms.append(str(exp["title"]))
    if data.get("current_company"):
        company_terms.insert(0, str(data["current_company"]))
    if data.get("current_title"):
        title_terms.insert(0, str(data["current_title"]))

    recent_company = data.get("current_company") or (company_terms[0] if company_terms else None)
    recent_title = data.get("current_title") or (title_terms[0] if title_terms else None)

    vector_parts = [
        _profile_text(data),
        recent_company,
        recent_title,
        *canonical_skills,
    ]
    doc["vector_text"] = _text(*vector_parts)
    return doc


def build_child_documents_c(data: dict) -> list[dict]:
    """变体 C 子 chunk：在变体 B 基础上，把项目名称与业务场景合入项目段。

    项目段 vector_text 额外携带 `name` 与 `business`/`domain`/`industry`，
    使项目名称和业务场景可参与语义召回；仍不新增生成模型调用。
    """
    children: list[dict] = []
    for exp in data.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        parts = [
            str(exp.get("company") or ""),
            str(exp.get("title") or ""),
            str(exp.get("summary") or ""),
        ]
        if not any(p.strip() for p in parts):
            continue
        tokens: list[str] = []
        for part in parts:
            tokens.extend(tokenize_lexical_text(part))
        children.append({
            "kind": "experience",
            "vector_text": _text(*parts),
            "keyword_index_text": " ".join(_uniq(tokens)),
        })
    for proj in data.get("projects") or []:
        if not isinstance(proj, dict):
            continue
        business = proj.get("business") or proj.get("domain") or proj.get("industry") or ""
        parts = [
            str(proj.get("name") or ""),
            str(business),
            str(proj.get("tech_stack") or ""),
            str(proj.get("summary") or ""),
        ]
        if not any(p.strip() for p in parts):
            continue
        tokens = []
        for part in parts:
            tokens.extend(tokenize_lexical_text(part))
        children.append({
            "kind": "project",
            "vector_text": _text(*parts),
            "keyword_index_text": " ".join(_uniq(tokens)),
        })
    return children


def build_candidate_document_c(
    data: dict,
    *,
    display_name: str | None = None,
    school_alias_groups: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """变体 C：父向量同变体 B，项目名称/业务场景由 build_child_documents_c 合入项目段。"""
    return build_candidate_document_b(
        data, display_name=display_name, school_alias_groups=school_alias_groups
    )
