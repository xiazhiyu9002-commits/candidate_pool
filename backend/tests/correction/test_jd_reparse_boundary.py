import pytest

from kerui_recruit.correction.service import CorrectionService
from kerui_recruit.db.models import Jd, JdRevision
from kerui_recruit.jd.pipeline import JdPipeline
from kerui_recruit.jd.structured import ParsedJd
from tests.correction.test_correction import session_factory
from tests.correction.test_linked_corrections import linked


@pytest.mark.asyncio
async def test_jd_is_processing_during_provider_and_manual_names_survive(session_factory, linked):
    _, _, jid, rid, _ = linked
    service = CorrectionService(session_factory)
    service.apply_correction(entity_type="jd", entity_id=jid, field_name="title", new_value="Manual role")
    class Parser:
        async def parse_jd(self, source):
            with session_factory() as session:
                assert session.get(JdRevision, rid).status == "PROCESSING"
            with pytest.raises(ValueError, match="解析"):
                service.apply_correction(entity_type="jd_revision", entity_id=rid,
                                         field_name="source_text", new_value="Concurrent replacement")
            return ParsedJd(title="Stale model role", company="Stale model company")
    await JdPipeline(session_factory=session_factory, parser=Parser()).run(rid)
    with session_factory() as session:
        revision = session.get(JdRevision, rid)
        assert session.get(Jd, jid).title == "Manual role"
        assert session.get(Jd, jid).company == "Example"
        assert revision.parsed_data["title"] == "Manual role"


@pytest.mark.asyncio
async def test_failed_new_source_parse_becomes_failed_and_retry_can_finish(session_factory, linked):
    _, _, _, rid, _ = linked
    CorrectionService(session_factory).apply_correction(entity_type="jd_revision", entity_id=rid,
        field_name="source_text", new_value="New source")
    class FailingParser:
        async def parse_jd(self, source):
            raise RuntimeError("controlled provider failure")
    pipeline = JdPipeline(session_factory=session_factory, parser=FailingParser())
    with pytest.raises(RuntimeError, match="controlled"):
        await pipeline.run(rid)
    with session_factory() as session:
        assert session.get(JdRevision, rid).status == "FAILED"
    class GoodParser:
        async def parse_jd(self, source):
            assert source == "New source"
            return ParsedJd(title="New extracted role")
    pipeline.parser = GoodParser()
    assert (await pipeline.run(rid)).status == "READY"


@pytest.mark.asyncio
async def test_changed_source_cannot_receive_older_parse_output(session_factory, linked):
    _, _, _, rid, _ = linked
    class ChangingParser:
        async def parse_jd(self, source):
            with session_factory() as session, session.begin():
                revision = session.get(JdRevision, rid)
                revision.source_text = "Newer committed source"
                revision.status = "PENDING"
                revision.parsed_data = None
            return ParsedJd(title="Older source extraction")
    with pytest.raises(RuntimeError, match="changed"):
        await JdPipeline(session_factory=session_factory, parser=ChangingParser()).run(rid)
    with session_factory() as session:
        revision = session.get(JdRevision, rid)
        assert revision.status == "PENDING"
        assert revision.parsed_data is None


@pytest.mark.asyncio
async def test_stale_parse_cannot_mark_newer_processing_source_failed(session_factory, linked):
    _, _, _, rid, _ = linked
    class NewerProcessingParser:
        async def parse_jd(self, source):
            with session_factory() as session, session.begin():
                revision = session.get(JdRevision, rid)
                revision.source_text = "Newer source already processing"
                revision.status = "PROCESSING"
            return ParsedJd(title="Stale extraction")
    with pytest.raises(RuntimeError, match="changed"):
        await JdPipeline(session_factory=session_factory, parser=NewerProcessingParser()).run(rid)
    with session_factory() as session:
        assert session.get(JdRevision, rid).status == "PROCESSING"
