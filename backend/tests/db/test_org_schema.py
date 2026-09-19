from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Company, Department, Employee
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_company_department_tree_and_reporting(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        company = Company(name="字节跳动")
        session.add(company)
        session.flush()

        tech = Department(company_id=company.id, name="技术部")
        session.add(tech)
        session.flush()

        backend = Department(company_id=company.id, parent_id=tech.id, name="后端组")
        session.add(backend)
        session.flush()

        lead = Employee(company_id=company.id, department_id=backend.id, name="张三", title="后端组长")
        session.add(lead)
        session.flush()

        report = Employee(
            company_id=company.id,
            department_id=backend.id,
            name="李四",
            title="后端工程师",
            report_to=lead.id,
            is_key=True,
        )
        session.add(report)
        session.commit()

        loaded = session.scalars(select(Company)).one()
        assert loaded.name == "字节跳动"
        assert len(loaded.departments) == 2

        loaded_lead = session.get(Employee, lead.id)
        assert loaded_lead is not None
        assert len(loaded_lead.subordinates) == 1
        assert loaded_lead.subordinates[0].name == "李四"

        loaded_report = session.get(Employee, report.id)
        assert loaded_report.report_to_employee.name == "张三"
        assert loaded_report.is_key is True
