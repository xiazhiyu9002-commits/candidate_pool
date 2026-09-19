from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.org.service import OrgService


@pytest.fixture
def service(tmp_path: Path) -> OrgService:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return OrgService(session_factory=factory)


def test_create_and_list_company(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    assert company.name == "字节跳动"
    assert [c.name for c in service.list_companies()] == ["字节跳动"]


def test_department_tree_and_flat_rows(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    tech = service.create_department(company_id=company.id, name="技术部")
    backend = service.create_department(
        company_id=company.id,
        name="后端组",
        parent_id=tech.id,
        team_size=8,
        business_direction="支付、风控",
        tech_stack="Java、Go",
        office_location="北京",
        hc_status="开放",
        hc_internal_note="Q3 预算待批",
    )

    lead = service.create_employee(
        company_id=company.id,
        department_id=backend.id,
        name="张三",
        title="后端组长",
        job_level="P7",
        is_key=True,
    )
    service.create_employee(
        company_id=company.id,
        department_id=backend.id,
        name="李四",
        title="后端工程师",
        job_level="P6",
        report_to=lead.id,
        subordinate_count=0,
        tenure_years=Decimal("3.5"),
        business_module="支付网关",
        status="在职",
        intention="待观察",
        remark="重点关注",
        contact="13800000000",
    )

    departments = service.list_departments(company.id)
    assert {d.name for d in departments} == {"技术部", "后端组"}

    employees = service.list_employees(company.id)
    assert {e.name for e in employees} == {"张三", "李四"}

    rows = service.flat_rows(company.id)
    # 员工两行 + 纯部门（技术部，无员工）一行。
    assert len(rows) == 3

    lisi = next(r for r in rows if r["name"] == "李四")
    assert lisi["company"] == "字节跳动"
    assert lisi["top_department"] == "技术部"
    assert lisi["sub_department"] == "后端组"
    assert lisi["report_to_name"] == "张三"
    assert lisi["leader_name"] == ""
    assert lisi["hc_status"] == "开放"
    assert lisi["hc_internal_note"] == "Q3 预算待批"
    assert lisi["tenure_years"] == 3.5
    assert lisi["intention"] == "待观察"


def test_department_leader_resolves_to_name(service: OrgService) -> None:
    company = service.create_company(name="腾讯")
    boss = service.create_employee(company_id=company.id, name="王五", title="技术总监")
    tech = service.create_department(
        company_id=company.id,
        name="技术部",
        leader_id=boss.id,
        leader_report_to=boss.id,
    )
    service.create_employee(company_id=company.id, department_id=tech.id, name="李四", title="工程师")

    rows = service.flat_rows(company.id)
    lisi = next(r for r in rows if r["name"] == "李四")
    assert lisi["leader_name"] == "王五"
    assert lisi["leader_report_to_name"] == "王五"


def test_create_employee_requires_valid_company_and_department(service: OrgService) -> None:
    company = service.create_company(name="阿里")
    other = service.create_company(name="腾讯")
    dept = service.create_department(company_id=other.id, name="技术部")

    with pytest.raises(LookupError):
        service.create_employee(company_id="missing", name="张三")

    # A department from a different company must be rejected.
    with pytest.raises(LookupError):
        service.create_employee(company_id=company.id, department_id=dept.id, name="张三")

    # Valid: no department, or a department that belongs to the company.
    ok = service.create_employee(company_id=company.id, name="李四")
    assert ok.name == "李四"


def test_update_and_delete_employee(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    employee = service.create_employee(company_id=company.id, name="张三", title="工程师")

    updated = service.update_employee(employee.id, title="高级工程师", job_level="P7")
    assert updated.title == "高级工程师"
    assert updated.job_level == "P7"
    assert updated.name == "张三"

    service.delete_employee(employee.id)
    assert service.list_employees(company.id) == []


def test_update_and_delete_department(service: OrgService) -> None:
    company = service.create_company(name="腾讯")
    dept = service.create_department(company_id=company.id, name="技术部")

    updated = service.update_department(dept.id, name="研发部", hc_status="开放")
    assert updated.name == "研发部"
    assert updated.hc_status == "开放"

    service.delete_department(dept.id)
    assert service.list_departments(company.id) == []


def test_delete_company_cascades(service: OrgService) -> None:
    company = service.create_company(name="阿里")
    dept = service.create_department(company_id=company.id, name="技术部")
    service.create_employee(company_id=company.id, department_id=dept.id, name="张三")

    service.delete_company(company.id)
    assert service.list_companies() == []
    assert service.list_departments(company.id) == []
    assert service.list_employees(company.id) == []


def test_arch_lines_folds_non_key_subordinates(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    dept = service.create_department(company_id=company.id, name="技术部", leader_id=None)

    boss = service.create_employee(company_id=company.id, department_id=dept.id, name="张三", title="研发总监")
    lead = service.create_employee(company_id=company.id, department_id=dept.id, name="李四", title="后端组长", report_to=boss.id)
    service.create_employee(company_id=company.id, department_id=dept.id, name="王五", title="后端工程师", report_to=lead.id)
    service.create_employee(company_id=company.id, department_id=dept.id, name="赵六", title="后端工程师", report_to=lead.id)

    lines = service.arch_lines(company.id)
    labels = [label for _, label in lines]
    assert "张三（研发总监）" in labels
    assert "李四（后端组长）" in labels
    # 王五 / 赵六 are not key positions, so they fold into +2.
    assert any(label.startswith("+") for label in labels)


def test_flat_rows_auto_computes_counts(service: OrgService) -> None:
    company = service.create_company(name="腾讯")
    dept = service.create_department(company_id=company.id, name="技术部")

    boss = service.create_employee(company_id=company.id, department_id=dept.id, name="张三")
    service.create_employee(company_id=company.id, department_id=dept.id, name="李四", report_to=boss.id)

    rows = service.flat_rows(company.id)
    boss_row = next(r for r in rows if r["name"] == "张三")
    assert boss_row["subordinate_count"] == 1
    assert boss_row["team_size"] == 2


def test_build_tree_combines_departments_and_reporting(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    tech = service.create_department(company_id=company.id, name="技术部")
    backend = service.create_department(company_id=company.id, name="后端组", parent_id=tech.id)

    boss = service.create_employee(company_id=company.id, department_id=backend.id, name="张三", title="技术总监")
    lead = service.create_employee(company_id=company.id, department_id=backend.id, name="李四", title="后端组长", report_to=boss.id)
    service.create_employee(company_id=company.id, department_id=backend.id, name="王五", title="工程师", report_to=lead.id)

    tree = service.build_tree(company.id)
    assert tree.kind == "company"
    assert tree.name == "字节跳动"

    tech_node = tree.children[0]
    assert tech_node.kind == "department"
    assert tech_node.name == "技术部"

    backend_node = tech_node.children[0]
    assert backend_node.kind == "department"
    assert backend_node.name == "后端组"
    assert backend_node.team_size == 3

    boss_node = backend_node.children[0]
    assert boss_node.kind == "employee"
    assert boss_node.name == "张三"
    assert boss_node.title == "技术总监"

    lead_node = boss_node.children[0]
    assert lead_node.name == "李四"
    assert lead_node.children[0].name == "王五"


def test_build_tree_does_not_duplicate_unassigned_leader(service: OrgService) -> None:
    company = service.create_company(name="得物")
    dept = service.create_department(company_id=company.id, name="算法平台")
    leader = service.create_employee(company_id=company.id, name="魏超", title="负责人")
    service.update_department(dept.id, leader_id=leader.id)

    tree = service.build_tree(company.id)
    ids: list[str] = []

    def walk(node) -> None:
        ids.append(node.id)
        for child in node.children:
            walk(child)

    walk(tree)
    assert ids.count(leader.id) == 1


def test_build_arch_tree_shows_departments_only(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    dept = service.create_department(company_id=company.id, name="技术部")

    boss = service.create_employee(company_id=company.id, department_id=dept.id, name="张三", title="技术总监")
    service.create_employee(company_id=company.id, department_id=dept.id, name="李四", title="后端组长", report_to=boss.id)
    service.create_employee(company_id=company.id, department_id=dept.id, name="王五", title="工程师", report_to=boss.id)
    service.update_department(dept.id, leader_id=boss.id)

    tree = service.build_arch_tree(company.id)
    dept_node = tree.children[0]
    assert dept_node.kind == "department"
    assert dept_node.name == "技术部"
    assert dept_node.leader_name == "张三"
    # 架构图只显示部门（负责人内联到标签），不再显示员工节点
    assert dept_node.children == []
