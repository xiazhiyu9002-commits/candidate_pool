import io
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.org.export import export_arch_pdf, export_client, export_internal
from kerui_recruit.org.service import OrgService


def _headers(payload: bytes) -> list[str]:
    workbook = load_workbook(io.BytesIO(payload))
    sheet = workbook.active
    return [cell.value for cell in sheet[1]]


@pytest.fixture
def service(tmp_path: Path) -> OrgService:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return OrgService(session_factory=factory)


def test_internal_export_contains_sensitive_columns() -> None:
    headers = _headers(export_internal([]))
    assert "跳槽意向" in headers
    assert "备注" in headers
    assert "联系方式" in headers
    assert "HC内部判断" in headers


def test_client_export_strips_sensitive_columns() -> None:
    headers = _headers(export_client([]))
    assert "人员姓名" in headers
    assert "HC状态" in headers
    assert "跳槽意向" not in headers
    assert "备注" not in headers
    assert "联系方式" not in headers
    assert "HC内部判断" not in headers


def test_export_arch_pdf_generates_card_pdf(service: OrgService) -> None:
    company = service.create_company(name="字节跳动")
    dept = service.create_department(company_id=company.id, name="技术部")
    service.create_employee(company_id=company.id, department_id=dept.id, name="张三", title="技术总监")

    root = service.build_arch_tree(company.id)
    payload = export_arch_pdf(root, orientation="vertical", watermark="内部资料")
    assert payload[:4] == b"%PDF"
    assert len(payload) > 200


def test_export_arch_pdf_shows_leader_and_omits_metadata(service: OrgService) -> None:
    import pymupdf

    company = service.create_company(name="字节跳动")
    led = service.create_department(company_id=company.id, name="技术部")
    noled = service.create_department(company_id=company.id, name="市场部")
    leader = service.create_employee(company_id=company.id, department_id=led.id, name="张三", title="技术总监")
    service.update_department(led.id, leader_id=leader.id)

    root = service.build_arch_tree(company.id)
    payload = export_arch_pdf(root, orientation="vertical")
    doc = pymupdf.open(stream=payload, filetype="pdf")

    assert doc.page_count == 1
    text = doc[0].get_text()
    assert "技术部-张三" in text
    assert "市场部-XXX" in text
    assert "人" not in text  # 不显示部门人数
    assert "技术总监" not in text  # 不显示职位等其他信息


def test_export_arch_pdf_fits_long_names(service: OrgService) -> None:
    import pymupdf

    company = service.create_company(name="北京字节跳动科技有限公司")
    dept = service.create_department(company_id=company.id, name="战略与商业发展事业部")
    leader = service.create_employee(company_id=company.id, department_id=dept.id, name="欧阳明轩", title="资深总监")
    service.update_department(dept.id, leader_id=leader.id)

    root = service.build_arch_tree(company.id)
    payload = export_arch_pdf(root, orientation="vertical")
    doc = pymupdf.open(stream=payload, filetype="pdf")
    text = doc[0].get_text()

    assert "北京字节跳动科技有限公司" in text
    assert "战略与商业发展事业部-欧阳明轩" in text
