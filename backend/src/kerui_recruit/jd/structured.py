from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from kerui_recruit.direction.policy import (
    MAX_CAREER_DIRECTIONS_JD,
    TAXONOMY_VERSION,
    normalize_business_directions,
    normalize_career,
    normalize_direction,
    sync_assessment,
)
from kerui_recruit.jd.profile_constraints import normalize_constraints


class ParsedJdRequirement(BaseModel):
    kind: str = Field(pattern="^(MUST|PLUS|EXCLUDE)$")
    label: str
    value: str


class ParsedJdMustSkillGroup(BaseModel):
    """一个必备技能组：组内 alternatives 为 OR，组之间为 AND。"""

    alternatives: list[str] = Field(default_factory=list)
    source_quote: str = ""


class ProfilePoint(BaseModel):
    """画像分点：一句要点 + 可追溯的结构化证据路径。"""

    text: str
    evidence_paths: list[str] = Field(default_factory=list)


class ExactConstraint(BaseModel):
    """人工 JD 硬条件：组内 alternatives 为 OR、组间 AND，strength=MUST|PLUS|EXCLUDE。"""

    kind: str
    operator: str = "OR"
    alternatives: list[str] = Field(default_factory=list)
    strength: str
    source: str = "manual"
    source_text: str = ""


class ParsedJd(BaseModel):
    title: str
    company: str = ""
    department: str | None = None
    location: str | None = None
    salary: str | None = None
    ai_category: str | None = Field(default=None, pattern="^(CORE_AI|AI_RELATED|NON_AI)$")
    industry: str | None = None
    min_years: float | None = Field(default=None, ge=0, le=80)
    highest_degree: str | None = None
    qs_level: str | None = None
    core_duties: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    plus_skills: list[str] = Field(default_factory=list)
    plus_industry: list[str] = Field(default_factory=list)
    plus_project_types: list[str] = Field(default_factory=list)
    summary: str = ""
    candidate_profile: str | None = None
    # 双形态候选人画像：整体段落 / 分点 / 浓缩上下文（与 candidate_profile 共享同一事实来源）。
    candidate_profile_narrative: str | None = None
    candidate_profile_points: list[ProfilePoint] = Field(default_factory=list)
    candidate_profile_compact: str | None = None
    candidate_profile_version: int = 3
    # 画像来源/幂等键/过期标记：与候选人侧 ai_profile_source/input_hash/stale 对称。
    # 必须声明为字段，否则重新解析时 model_dump 会丢掉回填写入的这三个键。
    candidate_profile_source: str | None = None
    candidate_profile_input_hash: str | None = None
    candidate_profile_stale: bool = False
    requirements: list[ParsedJdRequirement] = Field(default_factory=list)
    # 人工 JD 硬条件（卡 985、字节或阿里背景等），组内 OR、组间 AND。
    exact_constraints: list[ExactConstraint] = Field(default_factory=list)
    # 必备技能 AND/OR 组（组内 OR、组间 AND）；替代旧 required_skills 的硬条件语义。
    must_skill_groups: list[ParsedJdMustSkillGroup] = Field(default_factory=list)
    # 职业方向（技术岗粗分类），LLM 从固定枚举中选一，允许为空。
    direction: str | None = None
    # v2 方向评估：主/次方向、置信度、证据路径与管理属性（JSON）。
    direction_assessment: dict | None = None
    # 多值职业大类（1~3）。direction 为兼容字段，取其首个非 OTHER 值。
    career_directions: list[str] = Field(default_factory=list)
    # 职业方向细分（每个大类下 ≤2）。
    career_specializations: list[str] = Field(default_factory=list)
    # 业务方向（≤2），依据岗位职责/项目方向判定。
    business_directions: list[str] = Field(default_factory=list)
    # 词表版本号，由后端盖章（不依赖 LLM 输出）。
    career_taxonomy_version: str = TAXONOMY_VERSION

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, value: object) -> str | None:
        """非法枚举归待核（None），不直接入索引。"""
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
    def _normalize_multi_directions(self) -> "ParsedJd":
        """合法化 + 限流（JD 大类 ≤3、每大类下细分 ≤2、业务方向 ≤2），并镜像单值 direction。"""
        career, specs = normalize_career(
            self.career_directions, self.career_specializations,
            max_directions=MAX_CAREER_DIRECTIONS_JD,
        )
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

    @model_validator(mode="after")
    def _normalize_exact_constraints(self) -> "ParsedJd":
        """硬条件规整：非法取值丢弃、MUST 必须有原文依据（否则降级 PLUS）。

        解析期由模型产出、人工编辑由接口写入，两条路径共用同一份规整口径，
        保证匹配侧 evaluate_exact_constraints 拿到的一定是合法契约。
        """
        normalized = normalize_constraints(self.exact_constraints)
        self.exact_constraints = [ExactConstraint(**item) for item in normalized]
        return self


class ConstraintList(BaseModel):
    """硬条件抽取的结构化输出（「按文本重解析硬条件」用）。"""

    constraints: list[ExactConstraint] = Field(default_factory=list)


class JdParser(Protocol):
    async def parse_jd(self, text: str) -> ParsedJd: ...
    async def split_jds(self, text: str) -> list[str]: ...