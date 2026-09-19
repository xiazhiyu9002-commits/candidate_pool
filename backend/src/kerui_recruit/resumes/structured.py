from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kerui_recruit.direction.policy import (
    TAXONOMY_VERSION,
    normalize_business_directions,
    normalize_career,
    normalize_direction,
    sync_assessment,
)


class ParsedExperience(BaseModel):
    company: str | None = None
    title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    location: str | None = None
    summary: str | None = ""
    industry: str | None = None


class ParsedProject(BaseModel):
    name: str | None = None
    summary: str | None = ""
    tech_stack: str | list[str] | None = None
    business_scene: str | list[str] | None = None


class ParsedEducation(BaseModel):
    school: str | None = None
    degree: str | None = None
    major: str | None = None
    graduation_year: int | None = Field(default=None, ge=1900, le=2100)
    country_region: str | None = None
    school_tags: list[str] = Field(default_factory=list)
    qs_year: int | None = None
    qs_rank: int | None = Field(default=None, ge=1)


class ProfilePoint(BaseModel):
    """画像分点：一句要点 + 可追溯的结构化证据路径。"""

    text: str
    evidence_paths: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    name: str | None = None
    total_years: float | None = Field(default=None, ge=0, le=80)
    highest_degree: str | None = None
    location: str | None = None
    preferred_location: str | None = None
    preferred_locations: list[str] = Field(default_factory=list)
    school: str | None = None
    school_level: str | None = None
    qs_rank: int | None = Field(default=None, ge=1)
    school_tier: str | None = None
    graduation_year: int | None = Field(default=None, ge=1900, le=2100)
    birth_year: int | None = Field(default=None, ge=1950, le=2015)
    age: int | None = Field(default=None, ge=16, le=80)
    gender: str | None = None
    salary: str | None = None
    job_level: str | None = None
    industry: str | None = None
    current_industry: str | None = None
    longest_industry: str | None = None
    skills: list[str] = Field(default_factory=list)
    summary: str | None = ""
    experiences: list[ParsedExperience] = Field(default_factory=list)
    projects: list[ParsedProject] = Field(default_factory=list)
    educations: list[ParsedEducation] = Field(default_factory=list)
    current_company: str | None = None
    current_title: str | None = None
    location_source: str | None = None
    age_source: str | None = None
    ai_profile_summary: str | None = None
    ai_profile_source: str | None = None
    ai_profile_input_hash: str | None = None
    ai_profile_stale: bool = False
    # 双形态画像：整体段落 / 分点 / 浓缩上下文（与 ai_profile_summary 共享同一事实来源）。
    ai_profile_narrative: str | None = None
    ai_profile_points: list[ProfilePoint] = Field(default_factory=list)
    ai_profile_compact: str | None = None
    ai_profile_version: int = 3
    # 职业方向（技术岗粗分类），LLM 从固定枚举中选一，允许为空。
    direction: str | None = None
    # v2 方向评估：主/次方向、置信度、证据路径与管理属性（JSON）。
    direction_assessment: dict | None = None
    # 多值职业大类（≤2）。direction 为兼容字段，取其首个非 OTHER 值。
    career_directions: list[str] = Field(default_factory=list)
    # 职业方向细分（≤4，每个大类下 ≤2）。
    career_specializations: list[str] = Field(default_factory=list)
    # 业务方向（≤2），依据项目/经历的业务场景判定。
    business_directions: list[str] = Field(default_factory=list)
    # 词表版本号，由后端盖章（不依赖 LLM 输出）。
    career_taxonomy_version: str = TAXONOMY_VERSION

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, value: object) -> str | None:
        """非法枚举（如 OPERATIONS）归待核（None），不直接入索引。"""
        return normalize_direction(value if isinstance(value, str) else None)

    @field_validator(
        "career_directions", "career_specializations", "business_directions",
        mode="before",
    )
    @classmethod
    def _coerce_string_list(cls, value: object) -> list[object]:
        """容忍 LLM 输出标量字符串或 null。"""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    @model_validator(mode="after")
    def _normalize_multi_directions(self) -> "ParsedResume":
        """合法化 + 限流（大类 ≤2、每大类下细分 ≤2、业务方向 ≤2），并镜像单值 direction。"""
        career, specs = normalize_career(self.career_directions, self.career_specializations)
        self.career_directions = list(career)
        self.career_specializations = list(specs)
        self.business_directions = list(normalize_business_directions(self.business_directions))
        self.career_taxonomy_version = TAXONOMY_VERSION
        if self.direction is None:
            primary = next((d for d in self.career_directions if d != "OTHER"), None)
            if primary is not None:
                self.direction = primary
        self.direction_assessment = sync_assessment(
            self.direction_assessment, career, specs, tuple(self.business_directions)
        )
        return self


class NormalizedExperience(BaseModel):
    model_config = ConfigDict(frozen=True)

    company: str | None
    title: str | None
    start_date: str | None = None
    end_date: str | None = None
    location: str | None = None
    summary: str
    industry: str | None = None


class NormalizedProject(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str | None
    summary: str
    tech_stack: str | None = None
    business_scene: str | None = None


class NormalizedEducation(BaseModel):
    model_config = ConfigDict(frozen=True)

    school: str | None
    degree: str | None = None
    major: str | None = None
    graduation_year: int | None = None
    country_region: str | None = None
    school_tags: tuple[str, ...] = ()
    qs_year: int | None = None
    qs_rank: int | None = None


class NormalizedResume(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str | None
    total_years: Decimal | None
    highest_degree: str | None
    location: str | None
    preferred_location: str | None = None
    preferred_locations: tuple[str, ...] = ()
    school: str | None = None
    school_level: str | None = None
    qs_rank: int | None = None
    school_tier: str | None = None
    graduation_year: int | None = None
    birth_year: int | None = None
    age: int | None = None
    age_source: str | None = None
    gender: str | None = None
    salary: str | None = None
    job_level: str | None = None
    industry: str | None = None
    current_industry: str | None = None
    longest_industry: str | None = None
    skills: tuple[str, ...]
    summary: str
    experiences: tuple[NormalizedExperience, ...]
    projects: tuple[NormalizedProject, ...]
    educations: tuple[NormalizedEducation, ...] = ()
    current_company: str | None = None
    current_title: str | None = None
    location_source: str | None = None
    ai_profile_summary: str | None = None
    ai_profile_source: str | None = None
    ai_profile_input_hash: str | None = None
    ai_profile_stale: bool = False
    # 双形态画像：整体段落 / 分点 / 浓缩上下文（与 ai_profile_summary 共享同一事实来源）。
    ai_profile_narrative: str | None = None
    ai_profile_points: list[ProfilePoint] = Field(default_factory=list)
    ai_profile_compact: str | None = None
    ai_profile_version: int = 3
    # 职业方向（技术岗粗分类），从解析结果透传，供索引过滤。
    direction: str | None = None
    # v2 方向评估（主/次方向、置信度、证据路径与管理属性）。
    direction_assessment: dict | None = None
    # 多值职业大类（≤2）/ 细分（≤4，每大类下 ≤2）/ 业务方向（≤2）。
    career_directions: tuple[str, ...] = ()
    career_specializations: tuple[str, ...] = ()
    business_directions: tuple[str, ...] = ()
    career_taxonomy_version: str = TAXONOMY_VERSION


class ResumeParser(Protocol):
    async def parse_resume(self, text: str) -> ParsedResume: ...
