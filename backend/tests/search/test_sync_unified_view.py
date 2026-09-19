"""验证索引同步对同一候选人多修订写入统一硬字段（方向/地点）。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.sync import IndexSyncService


class _EmptySchoolRef:
    def __init__(self, *args, **kwargs):
        pass

    def alias_groups(self):
        return {}


class Embedding:
    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("kerui_recruit.schools.reference.SchoolReference", _EmptySchoolRef)
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        candidate = Candidate(display_name="Test", status="AVAILABLE")
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="unused")
        document = ResumeDocument(candidate=candidate)
        old = ResumeRevision(
            document=document, blob=blob, content_sha256="a" * 64, original_filename="old.pdf",
            status="READY", is_current=True,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            parsed_data={"name": "Test", "skills": ["Java"], "direction": "BACKEND", "location": "上海"},
        )
        new = ResumeRevision(
            document=document, blob=blob, content_sha256="b" * 64, original_filename="new.pdf",
            status="READY", is_current=True,
            created_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
            parsed_data={"name": "Test", "skills": ["Python", "SQL"], "direction": "DATA", "location": "北京",
                         "career_directions": ["DATA", "ALGORITHM"],
                         "career_specializations": ["DATA_WAREHOUSE", "ALGORITHM_RECSYS"],
                         "business_directions": ["INSURANCE", "MARKETING"]},
        )
        session.add_all([old, new])
        session.flush()
        cid = candidate.id
    index = LanceDBSearchIndex(tmp_path / "index", vector_dimension=2)
    service = IndexSyncService(session_factory=factory, index=index, embedding_provider=Embedding())
    yield factory, cid, service
    engine.dispose()


def test_snapshot_writes_unified_direction_across_revisions(setup):
    factory, cid, service = setup
    snapshot = service._snapshot("candidate", cid)
    assert snapshot is not None
    assert snapshot["documents"], "应有父/子 chunk"
    directions = {doc["direction"] for doc in snapshot["documents"]}
    assert directions == {"DATA"}, f"各 chunk 方向应统一为最近修订，实际 {directions}"
    locations = {doc["location"] for doc in snapshot["documents"]}
    assert locations == {"北京"}, f"各 chunk 地点应统一为最近修订，实际 {locations}"
    # 技能应跨修订合并（旧 Java + 新 Python/SQL）。
    merged_skills = set().union(*(doc.get("skills") or () for doc in snapshot["documents"]))
    assert {"Java", "Python"} <= merged_skills


def test_snapshot_keeps_per_revision_revision_ids(setup):
    factory, cid, service = setup
    snapshot = service._snapshot("candidate", cid)
    revision_ids = {doc["revision_id"] for doc in snapshot["documents"]}
    assert len(revision_ids) == 2, "应保留两份修订的 revision_id 以便追溯"


def test_snapshot_writes_unified_multi_value_directions_across_revisions(setup):
    """多值方向也是硬字段：各 chunk（含子切片）必须口径一致，否则筛选会漏。"""
    factory, cid, service = setup
    snapshot = service._snapshot("candidate", cid)
    assert snapshot is not None
    assert {tuple(doc["career_directions"]) for doc in snapshot["documents"]} == {("DATA", "ALGORITHM")}
    assert {tuple(doc["career_specializations"]) for doc in snapshot["documents"]} == {
        ("DATA_WAREHOUSE", "ALGORITHM_RECSYS")}
    assert {tuple(doc["business_directions"]) for doc in snapshot["documents"]} == {
        ("INSURANCE", "MARKETING")}


def test_candidate_view_falls_back_to_legacy_direction():
    """存量修订只有单值 direction + 旧专长时，统一视图须回退出多值字段。"""
    from kerui_recruit.match.candidate_view import build_candidate_view

    class _Revision:
        def __init__(self, rid, data, created):
            self.id = rid
            self.parsed_data = data
            self.created_at = created

    revision = _Revision(
        "r1",
        {"name": "Test", "direction": "BACKEND",
         "direction_assessment": {"primary": "BACKEND", "specializations": ["FULL_STACK"]}},
        datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    view = build_candidate_view([revision], None)
    assert view["career_directions"] == ["BACKEND"]
    assert view["career_specializations"] == ["FULL_STACK"]
    assert view["business_directions"] == []
