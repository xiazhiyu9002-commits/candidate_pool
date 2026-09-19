from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Jd, JdRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.jd.extract import split_jd_text
from kerui_recruit.jd.ingest import JdIngestService, IngestJd, JdIngestResult
from kerui_recruit.jd.pipeline import JdPipeline
from kerui_recruit.providers.local import LocalJdParser


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_ingest_creates_jd_and_revision(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        result = JdIngestService(session).ingest(
            IngestJd(company="某金融科技", title="Java 后端工程师", source_text="Java 3年 本科 金融")
        )

        assert isinstance(result, JdIngestResult)
        jd = session.scalars(select(Jd)).one()
        assert jd.company == "某金融科技"
        assert jd.status == "OPEN"
        assert jd.revisions[0].status == "PENDING"


def test_split_jd_text_splits_headers_and_falls_back() -> None:
    text = "岗位1：Java后端\n要求 Java 3年\n\n岗位2：算法工程师\n要求 Python"
    assert split_jd_text(text) == [
        "岗位1：Java后端\n要求 Java 3年",
        "岗位2：算法工程师\n要求 Python",
    ]
    assert split_jd_text("Java 后端工程师 3年 本科") == ["Java 后端工程师 3年 本科"]


@pytest.mark.asyncio
async def test_pipeline_parses_jd_into_revision(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        ingested = JdIngestService(session).ingest(
            IngestJd(company="某金融", title="Java", source_text="Java 后端 3年 本科 北京 金融支付")
        )

    pipeline = JdPipeline(session_factory=session_factory, parser=LocalJdParser())
    result = await pipeline.run(ingested.revision_id)

    assert result.status == "READY"
    with session_factory() as session:
        revision = session.get(JdRevision, ingested.revision_id)
        assert revision.parsed_data is not None
        assert revision.status == "READY"