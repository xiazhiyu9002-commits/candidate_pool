from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Candidate, IndexSyncRecord, Jd, TaskRecord
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.runtime import build_runtime


def test_migrated_database_requeues_entities_and_running_jobs_before_rebuilding(tmp_path):
    settings = Settings(data_root=tmp_path / "data", session_token="test-token")
    settings.paths.ensure()
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    with sessionmaker(engine)() as session, session.begin():
        candidate = Candidate(display_name="Synthetic")
        jd = Jd(company="Synthetic", title="Engineer")
        session.add_all([candidate, jd])
        session.add(TaskRecord(task_type="PARSE_RESUME", queue_name="normal", priority=1,
                               status="RUNNING", payload={}, idempotency_key="synthetic"))
    engine.dispose()
    marker = settings.paths.root / ".search-rebuild-required"
    marker.write_text("1")
    stale = settings.paths.search / "stale.bin"
    stale.write_bytes(b"old index")
    runtime = build_runtime(settings)
    assert not marker.exists()
    assert not stale.exists()
    with runtime.services.session_factory() as session:
        records = session.scalars(select(IndexSyncRecord)).all()
        assert {r.entity_type for r in records} == {"candidate", "jd"}
        assert all(r.requested_version > r.applied_version for r in records)
        assert session.scalar(select(TaskRecord)).status == "QUEUED"
