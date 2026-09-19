from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.diagnostics.service import DiagnosticsService


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_collect_reports_counts(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(Candidate(display_name="张三"))
        session.add(Jd(company="测试公司", title="Java"))
        session.commit()

    service = DiagnosticsService(
        session_factory=session_factory,
        database_path=tmp_path / "recruit.sqlite3",
    )
    diag = service.collect()
    assert diag["counts"]["candidates"] == 1
    assert diag["counts"]["jd"] == 1
    assert isinstance(diag["sqlite_version"], str)
    assert isinstance(diag["database_size_bytes"], int)


def test_export_json_returns_valid_bytes(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(Candidate(display_name="张三"))
        session.commit()

    service = DiagnosticsService(
        session_factory=session_factory,
        database_path=tmp_path / "recruit.sqlite3",
    )
    data = service.export_json()
    assert isinstance(data, bytes)
    assert b'"candidates"' in data
    assert b'"sqlite_version"' in data