from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Jd, JdRequirement, JdRevision
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_jd_revision_requirement_and_soft_delete_relationship(tmp_path: Path) -> None:
    """A JD without a versioned revision and structured requirements cannot be matched."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        jd = Jd(company="某金融科技公司", title="Java 后端工程师")
        revision = JdRevision(
            jd=jd,
            revision_no=1,
            source_text="负责支付系统，3年以上 Java，本科，北京",
            highest_degree="BACHELOR",
            min_years=Decimal("3.0"),
            location="北京",
            ai_category="AI_RELATED",
            is_current=True,
            requirements=[
                JdRequirement(kind="MUST", label="技能", value="Java"),
                JdRequirement(kind="PLUS", label="行业", value="金融"),
            ],
        )
        session.add(jd)
        session.commit()

        loaded = session.scalars(select(JdRevision)).one()
        assert loaded.jd.company == "某金融科技公司"
        assert loaded.jd.title == "Java 后端工程师"
        assert loaded.jd.deleted_at is None
        assert loaded.min_years == Decimal("3.0")
        assert {req.kind for req in loaded.requirements} == {"MUST", "PLUS"}


def test_jd_status_defaults_to_draft(tmp_path: Path) -> None:
    """A freshly created JD must start in DRAFT until explicitly opened."""
    with make_session(tmp_path / "recruit.sqlite3") as session:
        jd = Jd(company="A", title="B")
        session.add(jd)
        session.commit()

        assert jd.status == "DRAFT"