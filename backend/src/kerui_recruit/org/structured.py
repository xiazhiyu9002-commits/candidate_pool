from __future__ import annotations

from pydantic import BaseModel, Field


class ParsedOrgEmployee(BaseModel):
    name: str
    alias: str | None = None
    title: str | None = None
    job_level: str | None = None
    report_to_name: str | None = None
    department_name: str | None = None
    subordinate_count: int | None = None
    team_size: int | None = None
    remark: str | None = None


class ParsedOrgDepartment(BaseModel):
    name: str
    parent_name: str | None = None
    leader_name: str | None = None
    team_size: int | None = None
    business_direction: str | None = None


class ParsedOrgDraft(BaseModel):
    company_name: str
    departments: list[ParsedOrgDepartment] = Field(default_factory=list)
    employees: list[ParsedOrgEmployee] = Field(default_factory=list)


class OrgClarificationQuestion(BaseModel):
    question: str
    field: str | None = None
    hint: str | None = None


class OrgParseResult(BaseModel):
    draft: ParsedOrgDraft | None = None
    questions: list[OrgClarificationQuestion] = Field(default_factory=list)
