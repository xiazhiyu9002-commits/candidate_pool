from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from kerui_recruit.resumes.structured import (
    NormalizedEducation,
    NormalizedExperience,
    NormalizedProject,
    NormalizedResume,
    ParsedResume,
    ProfilePoint,
)
from kerui_recruit.search.degrees import DEGREE_ORDER, normalize_degree


_SCHOOL_LEVEL_PRIORITY = {"985": 4, "211": 3, "双一流": 2, "海外": 1, "普通": 0}


def derive_education_compat(educations: list) -> dict:
    """从结构化教育经历派生兼容字段，供编辑教育经历后重算展示/过滤值。"""
    best_degree: str | None = None
    best_rank = -1
    best_school: str | None = None
    bachelor_year: int | None = None
    fallback_year: int | None = None
    school_level: str | None = None
    level_rank = -1
    qs_rank: int | None = None

    for edu in educations:
        if not isinstance(edu, dict):
            continue
        degree = normalize_degree(edu.get("degree"))
        if degree and degree in DEGREE_ORDER:
            rank = DEGREE_ORDER.index(degree)
            if rank > best_rank:
                best_rank = rank
                best_degree = degree
                best_school = edu.get("school") if edu.get("school") else None
        if degree == "BACHELOR" and edu.get("graduation_year"):
            bachelor_year = int(edu["graduation_year"])
        if edu.get("graduation_year") and fallback_year is None:
            fallback_year = int(edu["graduation_year"])
        for tag in edu.get("school_tags") or []:
            rank = _SCHOOL_LEVEL_PRIORITY.get(str(tag), -1)
            if rank > level_rank:
                level_rank = rank
                school_level = str(tag)
        if edu.get("qs_rank"):
            qs_rank = int(edu["qs_rank"]) if qs_rank is None else min(qs_rank, int(edu["qs_rank"]))

    return {
        "highest_degree": best_degree,
        "school": best_school,
        "graduation_year": bachelor_year if bachelor_year is not None else fallback_year,
        "school_level": school_level,
        "qs_rank": qs_rank,
    }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def normalize_gender(value: str | None) -> str | None:
    """把性别统一为 男 / 女，兼容「男性/女士/先生」等常见写法。"""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    if cleaned.casefold() in {"男", "男性", "男生", "先生", "male", "m"}:
        return "男"
    if cleaned.casefold() in {"女", "女性", "女生", "女士", "female", "f"}:
        return "女"
    return cleaned


def _join_value(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return " / ".join(str(v).strip() for v in value if str(v).strip()) or None
    return _clean(value)


def _unique_skills(skills: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for skill in skills:
        cleaned = _clean(skill)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def _bachelor_graduation_year(educations: list) -> int | None:
    for education in educations:
        if normalize_degree(getattr(education, "degree", None)) == "BACHELOR" and education.graduation_year:
            return education.graduation_year
    return None


def _compute_age(
    age: int | None,
    birth_year: int | None,
    educations: list,
    legacy_graduation_year: int | None,
) -> tuple[int | None, str]:
    """按优先级派生年龄，并返回来源：explicit / birth_year / bachelor_graduation / unknown。"""
    current_year = datetime.now().year
    if age is not None:
        return age, "explicit"
    if birth_year is not None:
        return current_year - birth_year, "birth_year"
    bachelor_grad = _bachelor_graduation_year(educations)
    if bachelor_grad is not None:
        return current_year - bachelor_grad + 22, "bachelor_graduation"
    if not educations and legacy_graduation_year is not None:
        # 无结构化教育经历时，兼容字段按历史约定视为本科毕业年份。
        return current_year - legacy_graduation_year + 22, "bachelor_graduation"
    return None, "unknown"


def _recent_experience(experiences: list):
    if not experiences:
        return None
    present_markers = ("至今", "present", "now", "当前")
    for experience in experiences:
        end = (experience.end_date or "").strip().casefold()
        if any(marker in end for marker in present_markers):
            return experience
    return experiences[0]


def _derive_location(parsed: ParsedResume) -> tuple[str | None, str]:
    location = _clean(parsed.location)
    if location:
        return location, "resume"
    recent = _recent_experience(parsed.experiences)
    if recent is not None and _clean(recent.location):
        return _clean(recent.location), "recent_experience"
    return None, "unknown"


def normalize_resume(parsed: ParsedResume) -> NormalizedResume:
    degree = _clean(parsed.highest_degree)
    normalized_degree = normalize_degree(degree)
    if degree and normalized_degree is None:
        normalized_degree = degree.upper()
    years = (
        Decimal(str(parsed.total_years)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if parsed.total_years is not None
        else None
    )
    age, age_source = _compute_age(parsed.age, parsed.birth_year, parsed.educations, parsed.graduation_year)
    location, location_source = _derive_location(parsed)
    recent = _recent_experience(parsed.experiences)
    return NormalizedResume(
        name=_clean(parsed.name),
        total_years=years,
        highest_degree=normalized_degree,
        location=location,
        preferred_location=_clean(parsed.preferred_location),
        preferred_locations=_unique_skills(parsed.preferred_locations),
        school=_clean(parsed.school),
        school_level=_clean(parsed.school_level),
        qs_rank=parsed.qs_rank,
        school_tier=_clean(parsed.school_tier),
        graduation_year=parsed.graduation_year,
        birth_year=parsed.birth_year,
        age=age,
        age_source=age_source,
        gender=normalize_gender(parsed.gender),
        salary=_clean(parsed.salary),
        job_level=_clean(parsed.job_level),
        industry=_clean(parsed.industry),
        current_industry=_clean(parsed.current_industry),
        longest_industry=_clean(parsed.longest_industry),
        skills=_unique_skills(parsed.skills),
        summary=_clean(parsed.summary) or "",
        experiences=tuple(
            NormalizedExperience(
                company=_clean(experience.company),
                title=_clean(experience.title),
                start_date=_clean(experience.start_date),
                end_date=_clean(experience.end_date),
                location=_clean(experience.location),
                summary=_clean(experience.summary) or "",
                industry=_clean(experience.industry),
            )
            for experience in parsed.experiences
        ),
        projects=tuple(
            NormalizedProject(
                name=_clean(project.name),
                summary=_clean(project.summary) or "",
                tech_stack=_join_value(project.tech_stack),
                business_scene=_join_value(project.business_scene),
            )
            for project in parsed.projects
        ),
        educations=tuple(
            NormalizedEducation(
                school=_clean(education.school),
                degree=normalize_degree(_clean(education.degree)) or _clean(education.degree),
                major=_clean(education.major),
                graduation_year=education.graduation_year,
                country_region=_clean(education.country_region),
                school_tags=tuple(_clean(tag) for tag in education.school_tags if _clean(tag)),
                qs_year=education.qs_year,
                qs_rank=education.qs_rank,
            )
            for education in parsed.educations
        ),
        current_company=(
            _clean(parsed.current_company)
            or (_clean(recent.company) if recent is not None else None)
        ),
        current_title=(
            _clean(parsed.current_title)
            or (_clean(recent.title) if recent is not None else None)
        ),
        location_source=location_source,
        ai_profile_summary=_clean(parsed.ai_profile_summary),
        ai_profile_source=parsed.ai_profile_source,
        ai_profile_input_hash=parsed.ai_profile_input_hash,
        ai_profile_stale=parsed.ai_profile_stale,
        # 双形态画像：整体段落（与 ai_profile_summary 同源）、分点、浓缩上下文。
        ai_profile_narrative=_clean(parsed.ai_profile_narrative) or _clean(parsed.ai_profile_summary),
        ai_profile_points=[
            ProfilePoint(text=_clean(p.text), evidence_paths=[e for e in p.evidence_paths if e])
            for p in parsed.ai_profile_points
            if _clean(p.text)
        ],
        ai_profile_compact=_clean(parsed.ai_profile_compact),
        direction=parsed.direction,
        direction_assessment=parsed.direction_assessment,
        # 多值方向：ParsedResume 已做合法化与限流，此处只做去重与元组化。
        career_directions=tuple(dict.fromkeys(parsed.career_directions)),
        career_specializations=tuple(dict.fromkeys(parsed.career_specializations)),
        business_directions=tuple(dict.fromkeys(parsed.business_directions)),
        career_taxonomy_version=parsed.career_taxonomy_version,
    )
