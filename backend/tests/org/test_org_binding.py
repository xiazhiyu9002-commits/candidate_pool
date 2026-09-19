from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, CandidateContact
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.org.binding import OrgBindingService
from kerui_recruit.org.service import OrgService


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    return factory, encryption


def _create_candidate(factory, encryption, name: str, phone: str) -> str:
    with factory() as session:
        candidate = Candidate(display_name=name, status="AVAILABLE")
        session.add(candidate)
        session.flush()
        session.add(CandidateContact(candidate_id=candidate.id, phone_encrypted=encryption.encrypt(phone)))
        session.commit()
        return candidate.id


def _create_employee(org: OrgService, name: str):
    company = org.create_company(name="得物")
    employee = org.create_employee(company_id=company.id, name=name)
    return employee


def test_bind_matches_candidate_by_phone(env) -> None:
    factory, encryption = env
    candidate_id = _create_candidate(factory, encryption, "张三", "13800138000")
    org = OrgService(session_factory=factory)
    employee = _create_employee(org, "张三")
    binding = OrgBindingService(session_factory=factory, encryption=encryption)

    result = binding.bind(employee_id=employee.id, phone="13800138000", name="张三")

    assert result["matched"] is True
    assert result["candidate_id"] == candidate_id
    assert result["candidate_name"] == "张三"
    assert result["name_mismatch"] is False


def test_bind_flags_name_mismatch(env) -> None:
    factory, encryption = env
    _create_candidate(factory, encryption, "张三", "13800138000")
    org = OrgService(session_factory=factory)
    employee = _create_employee(org, "贺喜")
    binding = OrgBindingService(session_factory=factory, encryption=encryption)

    result = binding.bind(employee_id=employee.id, phone="13800138000", name="贺喜")

    assert result["matched"] is True
    assert result["candidate_name"] == "张三"
    assert result["name_mismatch"] is True


def test_bind_unmatched_keeps_employee_unbound(env) -> None:
    factory, encryption = env
    org = OrgService(session_factory=factory)
    employee = _create_employee(org, "李四")
    binding = OrgBindingService(session_factory=factory, encryption=encryption)

    result = binding.bind(employee_id=employee.id, phone="19900000000", name="李四")

    assert result["matched"] is False
    assert result["candidate_id"] is None

    reloaded = org.list_employees(employee.company_id)[0]
    assert reloaded.candidate_id is None
    assert reloaded.phone_encrypted is not None


def test_bind_requires_phone(env) -> None:
    factory, encryption = env
    org = OrgService(session_factory=factory)
    employee = _create_employee(org, "王五")
    binding = OrgBindingService(session_factory=factory, encryption=encryption)

    with pytest.raises(ValueError):
        binding.bind(employee_id=employee.id, phone="", name="王五")
