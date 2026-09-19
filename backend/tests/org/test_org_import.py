from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.org.service import OrgService
from kerui_recruit.org.structured import (
    ParsedOrgDepartment,
    ParsedOrgDraft,
    ParsedOrgEmployee,
)


@pytest.fixture
def service(tmp_path: Path) -> OrgService:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return OrgService(session_factory=factory)


def test_import_draft_creates_tree_and_reporting(service: OrgService) -> None:
    company = service.create_company(name="得物")

    draft = ParsedOrgDraft(
        company_name="得物",
        departments=[
            ParsedOrgDepartment(name="算法平台"),
            ParsedOrgDepartment(name="社区算法", parent_name="算法平台", leader_name="叶程", team_size=70),
        ],
        employees=[
            ParsedOrgEmployee(
                name="贺喜",
                alias="叶程",
                title="社区算法负责人",
                job_level="4-1",
                department_name="社区算法",
                subordinate_count=70,
                remark="抖音模型负责人，23年加入得物",
            ),
            ParsedOrgEmployee(
                name="王锐",
                title="商品推荐负责人",
                report_to_name="贺喜",
                department_name="社区算法",
                team_size=20,
            ),
        ],
    )

    result = service.import_draft(company.id, draft)
    assert result == {"departments": 2, "employees": 2}

    departments = service.list_departments(company.id)
    by_name = {d.name: d for d in departments}
    assert by_name["社区算法"].parent_id == by_name["算法平台"].id
    assert by_name["社区算法"].team_size == 70
    assert by_name["社区算法"].leader_id is not None

    employees = service.list_employees(company.id)
    by_employee_name = {e.name: e for e in employees}
    assert by_employee_name["王锐"].report_to == by_employee_name["贺喜"].id
    assert "花名：叶程" in by_employee_name["贺喜"].remark
    assert "抖音模型负责人" in by_employee_name["贺喜"].remark
    assert "团队规模约20人" in by_employee_name["王锐"].remark


def test_import_draft_rejects_missing_company(service: OrgService) -> None:
    draft = ParsedOrgDraft(company_name="不存在", departments=[], employees=[])
    with pytest.raises(LookupError):
        service.import_draft("missing-company", draft)


def test_import_draft_accumulates_source_text(service: OrgService) -> None:
    company = service.create_company(name="得物")

    service.import_draft(
        company.id,
        ParsedOrgDraft(company_name="得物", departments=[], employees=[]),
        source_text="第一次导入原文",
    )
    service.import_draft(
        company.id,
        ParsedOrgDraft(company_name="得物", departments=[], employees=[]),
        source_text="第二次导入原文",
    )

    stored = next(c for c in service.list_companies() if c.id == company.id).source_text
    assert stored == "第一次导入原文\n\n第二次导入原文"
