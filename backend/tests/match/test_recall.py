"""双通道召回 merge_recall_lanes 的单元测试 + 方向待核救援集成测试。"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.base import Base
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.recall import merge_recall_lanes
from kerui_recruit.match.service import MatchService
from kerui_recruit.providers.fakes import FakeRerankerProvider
from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


def _hit(cid):
    return SimpleNamespace(candidate_id=cid)


def test_merge_same_first_then_rescue_deduped():
    same = [_hit("a"), _hit("b")]
    rescue = [_hit("a"), _hit("c"), _hit("d")]
    merged = merge_recall_lanes(same, rescue, key=lambda h: h.candidate_id, limit=10, rescue_slots=30)
    assert [h.candidate_id for h in merged] == ["a", "b", "c", "d"]


def test_merge_caps_rescue_slots():
    same = [_hit("a")]
    rescue = [_hit("b"), _hit("c"), _hit("d")]
    merged = merge_recall_lanes(same, rescue, key=lambda h: h.candidate_id, limit=10, rescue_slots=1)
    assert [h.candidate_id for h in merged] == ["a", "b"]


def test_merge_respects_limit():
    same = [_hit(f"a{i}") for i in range(5)]
    rescue = [_hit(f"b{i}") for i in range(5)]
    merged = merge_recall_lanes(same, rescue, key=lambda h: h.candidate_id, limit=7, rescue_slots=30)
    assert len(merged) == 7


class FixedEmbedding:
    async def embed_query(self, text):
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_match_jd_rescues_unconfirmed_direction_frontend_candidates(tmp_path):
    engine = create_engine_for(tmp_path / "test.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    candidate_index = LanceDBSearchIndex(tmp_path / "candidates", vector_dimension=2)
    with factory.begin() as session:
        for i, cid in enumerate(("f1", "f2", "f3")):
            sha = chr(97 + i) * 64
            session.add(Candidate(id=cid, display_name=cid, status="AVAILABLE", total_years=Decimal("6")))
            session.add(Blob(id=f"blob-{cid}", content_sha256=sha, suffix=".txt", size_bytes=20, storage_path=f"blob-{cid}"))
            session.flush()
            session.add(ResumeDocument(id=f"doc-{cid}", candidate_id=cid))
            session.flush()
            session.add(ResumeRevision(id=f"rev-{cid}", document_id=f"doc-{cid}", blob_id=f"blob-{cid}",
                                       content_sha256=sha, original_filename="r.txt", status="READY", is_current=True,
                                       raw_text="React TypeScript 前端",
                                       parsed_data={"skills": ["React", "TypeScript"], "direction": None}))
        session.add(Jd(id="jd-f", company="f", title="前端开发", status="OPEN"))
        session.flush()
        session.add(JdRevision(id="rev-f", jd_id="jd-f", status="READY", is_current=True,
                               source_text="React 前端", min_years=Decimal("3"),
                               parsed_data={"summary": "前端开发", "required_skills": ["React"], "direction": "FRONTEND"}))
    candidate_index.upsert([
        SearchChunk(f"chunk-{cid}", cid, f"rev-{cid}", "React TypeScript 前端", (1.0, 0.0),
                    6, None, None, "AVAILABLE", direction=None)
        for cid in ("f1", "f2", "f3")
    ])
    search = HybridSearchService(index=candidate_index, embedding_provider=FixedEmbedding(),
                                 reranker_provider=FakeRerankerProvider())
    matcher = MatchService(session_factory=factory, search_service=search)
    page = await matcher.match_jd(revision_id="rev-f", limit=20, mode="keyword")
    assert {hit.candidate_id for hit in page.items} == {"f1", "f2", "f3"}

