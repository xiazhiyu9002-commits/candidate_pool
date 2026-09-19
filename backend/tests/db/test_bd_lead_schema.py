from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import BdLead
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_bd_lead_persists_with_defaults(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        lead = BdLead(
            source="web",
            company_name="字节跳动",
            raw_snippet="招聘 Java 工程师",
        )
        session.add(lead)
        session.commit()

        loaded = session.scalars(select(BdLead)).one()
        assert loaded.company_name == "字节跳动"
        assert loaded.status == "新线索"
        assert loaded.source == "web"