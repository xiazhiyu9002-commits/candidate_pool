import importlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk
from kerui_recruit.search.lancedb_index import (
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)


def maintenance():
    name = "kerui_recruit.search.rebuild_maintenance"
    assert importlib.util.find_spec(name) is not None, "safe maintenance rebuild entry point is missing"
    return importlib.import_module(name)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="unused")
        candidate = Candidate(display_name="Projection Test")
        session.add(ResumeRevision(document=ResumeDocument(candidate=candidate), blob=blob,
            original_filename="unused.pdf", content_sha256="a" * 64, status="READY", is_current=True,
            parsed_data={"name": "Projection Test", "skills": ["Python"], "preferred_locations": ["广州"]}))
        job = Jd(title="Python engineer", company="Example", status="OPEN")
        session.add(JdRevision(jd=job, source_text="Python backend role", status="READY", is_current=True))
        session.add(Candidate(display_name="not eligible", status="ARCHIVED"))
    engine.dispose()
    old = tmp_path / "old-index"
    old.mkdir()
    (old / "legacy.marker").write_bytes(b"legacy unrebuildable but recoverable")
    return path, old


def test_dry_run_never_creates_or_changes_source_or_old_index(source, tmp_path):
    path, old = source
    before = path.read_bytes()
    result = maintenance().diagnose(path, old, embedding_model="local-hash-v1", dimension=64)
    assert result["mode"] == "dry-run"
    assert result["target_metadata"] == {"embedding_model": "local-hash-v1", "vector_dimension": 64,
                                         "schema_version": INDEX_SCHEMA_VERSION,
                                         "chunk_version": INDEX_CHUNK_VERSION}
    assert result["counts"] == {"candidate": 2, "jd": 1}
    assert result["candidate_index"]["compatibility"] == "missing-metadata-or-empty"
    assert path.read_bytes() == before
    assert sorted(p.name for p in old.iterdir()) == ["legacy.marker"]


@pytest.mark.asyncio
async def test_build_uses_snapshot_and_preserves_old_index(source, tmp_path):
    path, old = source
    before = path.read_bytes()
    output = tmp_path / "staged"
    result = await maintenance().build_offline(path, old, output, app_stopped=True)
    assert result["status"] == "READY_FOR_OFFLINE_SWITCH"
    assert result["validated"] == {"candidate_entities": 1, "candidate_chunks": 1, "jd_entities": 1, "jd_chunks": 1}
    rebuilt = LanceDBSearchIndex(output / "search", vector_dimension=64,
                                embedding_model="local-hash-v1")
    assert rebuilt.is_ready()
    assert len(rebuilt.filter_search(CandidateFilters(preferred_locations=("广州",)), 10)) == 1
    assert rebuilt.search_fts("Python", CandidateFilters(), 10)
    assert (output / "search" / "jobs" / "candidate-index-metadata.json").exists()
    assert json.loads((output / "manifest.json").read_text())["status"] == "READY_FOR_OFFLINE_SWITCH"
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM index_sync").fetchone() == (0,)
    assert (old / "legacy.marker").read_bytes() == b"legacy unrebuildable but recoverable"


@pytest.mark.asyncio
async def test_remote_build_uses_injected_provider_and_writes_remote_metadata(source, tmp_path):
    from kerui_recruit.providers.fakes import FakeEmbeddingProvider
    path, old = source
    output = tmp_path / "staged-remote"
    result = await maintenance().build_offline(path, old, output, app_stopped=True,
        embedding_provider=FakeEmbeddingProvider(dimension=1024),
        embedding_model="BAAI/bge-m3", dimension=1024)
    assert result["status"] == "READY_FOR_OFFLINE_SWITCH"
    assert result["validated"] == {"candidate_entities": 1, "candidate_chunks": 1,
                                   "jd_entities": 1, "jd_chunks": 1}
    rebuilt = LanceDBSearchIndex(output / "search", vector_dimension=1024,
                                 embedding_model="BAAI/bge-m3")
    assert rebuilt.is_ready()
    assert len(rebuilt.filter_search(CandidateFilters(preferred_locations=("广州",)), 10)) == 1
    metadata = json.loads((output / "search" / "candidate-index-metadata.json").read_text())
    assert metadata == {"schema_version": INDEX_SCHEMA_VERSION, "embedding_model": "BAAI/bge-m3",
                        "vector_dimension": 1024, "chunk_version": INDEX_CHUNK_VERSION}


@pytest.mark.asyncio
async def test_remote_build_requires_injected_provider(source, tmp_path):
    path, old = source
    with pytest.raises(ValueError, match="injected embedding provider"):
        await maintenance().build_offline(path, old, tmp_path / "remote-no-provider",
                                          app_stopped=True, embedding_model="BAAI/bge-m3", dimension=1024)


@pytest.mark.asyncio
async def test_failed_build_cannot_be_promoted(source, tmp_path):
    path, old = source
    class FailureEmbedding:
        async def embed_documents(self, texts):
            raise RuntimeError("controlled offline failure")
    output = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="failed"):
        await maintenance().build_offline(path, old, output, app_stopped=True, embedding_provider=FailureEmbedding())
    assert json.loads((output / "manifest.json").read_text())["status"] == "FAILED"
    assert (old / "legacy.marker").exists()


@pytest.mark.asyncio
async def test_build_requires_shutdown_and_cannot_write_inside_old_index(source, tmp_path):
    path, old = source
    with pytest.raises(ValueError, match="stopped"):
        await maintenance().build_offline(path, old, tmp_path / "not-created", app_stopped=False)
    with pytest.raises(ValueError, match="overlap"):
        await maintenance().build_offline(path, old, old / "staging", app_stopped=True)
    assert not (tmp_path / "not-created").exists()


@pytest.mark.asyncio
async def test_source_write_during_build_invalidates_ready_manifest(source, tmp_path):
    from kerui_recruit.providers.local import LocalHashEmbeddingProvider
    path, old = source
    class ChangingSourceEmbedding(LocalHashEmbeddingProvider):
        async def embed_documents(self, texts):
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE candidate SET display_name='changed while rebuilding'")
            return await super().embed_documents(texts)
    output = tmp_path / "changed"
    with pytest.raises(RuntimeError, match="changed during build"):
        await maintenance().build_offline(path, old, output, app_stopped=True,
                                           embedding_provider=ChangingSourceEmbedding(dimension=64))
    assert json.loads((output / "manifest.json").read_text())["status"] == "FAILED"
    assert (old / "legacy.marker").exists()


def test_cli_diagnosis_supports_remote_and_execution_requires_credentials(source, tmp_path, capsys, monkeypatch):
    path, old = source
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    command = ["--database", str(path), "--index-root", str(old), "--model", "BAAI/bge-m3", "--dimension", "1024"]
    assert maintenance().main(command) == 0
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["mode"] == "dry-run" and diagnostic["execution_supported"] is True
    output = tmp_path / "remote-not-created"
    assert maintenance().main(command + ["--execute", "--app-stopped", "--output", str(output)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"
    assert not output.exists()


@pytest.mark.asyncio
async def test_rebuild_refuses_to_migrate_source_schema(source, tmp_path):
    path, old = source
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_version SET version=6")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="separate migration"):
        await maintenance().build_offline(path, old, tmp_path / "old-schema", app_stopped=True)
    assert path.read_bytes() == before
    assert not (tmp_path / "old-schema").exists()


def _direction_json(code: str = "BACKEND") -> dict:
    return {}


@pytest.fixture
def migrate_source(tmp_path):
    path = tmp_path / "migrate.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        blob = Blob(content_sha256="c" * 64, suffix=".pdf", size_bytes=1, storage_path="unused")
        candidate = Candidate(display_name="向量复用候选人")
        rev = ResumeRevision(document=ResumeDocument(candidate=candidate), blob=blob,
            original_filename="resume.pdf", content_sha256="c" * 64, status="READY", is_current=True,
            parsed_data={"name": "向量复用候选人", "skills": ["Python"], "direction_profile": _direction_json("BACKEND")})
        session.add(rev)
        job = Jd(title="后端工程师", company="Example", status="OPEN")
        jd_rev = JdRevision(jd=job, source_text="后端", status="READY", is_current=True,
            parsed_data={"direction_profile": _direction_json("BACKEND")})
        session.add(jd_rev)
        session.flush()
        candidate_id = candidate.id
        revision_id = rev.id
        jd_id = job.id
        jd_revision_id = jd_rev.id
    engine.dispose()

    old = tmp_path / "old-index"
    old.mkdir()
    candidate_index = LanceDBSearchIndex(old, vector_dimension=64,
                                         embedding_model="local-hash-v1")
    candidate_index.upsert([SearchChunk(
        id=f"{candidate_id}-0", candidate_id=candidate_id, revision_id=revision_id,
        content="Python 后端", vector=tuple([0.1] * 64), total_years=5, highest_degree=None,
        location=None, candidate_status="AVAILABLE")])
    candidate_index.optimize_pending()
    jobs = JdSearchIndex(old / "jobs", vector_dimension=64,
                          embedding_model="local-hash-v1")
    jobs.upsert(jd_id=jd_id, revision_id=jd_revision_id, content="后端工程师",
                vector=[0.2] * 64)
    jobs.index.optimize_pending()
    return path, old, candidate_id, revision_id, jd_id, jd_revision_id


def test_migrate_reuse_preserves_vectors(migrate_source, tmp_path):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staging = tmp_path / "staging-reuse"
    result = maintenance().migrate_reusing_vectors(
        path, old, staging, embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)

    assert result["status"] == "READY_FOR_OFFLINE_SWITCH"
    assert result["reused_vectors"] is True
    validated = result["validated"]
    assert validated["candidate_chunks"] == 1 and validated["candidate_entities"] == 1
    assert validated["jd_chunks"] == 1 and validated["jd_entities"] == 1
    assert validated["candidate_vector_checksum_match"] is True
    assert validated["jd_vector_checksum_match"] is True
    assert validated["sampled_vector_compare"]["all_match"] is True

    staged = LanceDBSearchIndex(staging, vector_dimension=64,
                                 embedding_model="local-hash-v1")
    rows = staged.get_revision_chunks(revision_id)
    assert len(rows) == 1
    assert rows[0]["vector"] == pytest.approx([0.1] * 64)
    # 源索引未被修改。
    source_rows = LanceDBSearchIndex(old, vector_dimension=64,
                                     embedding_model="local-hash-v1").get_revision_chunks(revision_id)
    assert source_rows[0]["vector"] == pytest.approx([0.1] * 64)


def test_migrate_reuse_refuses_incompatible_source(migrate_source, tmp_path):
    path, old, *_ = migrate_source
    # 破坏源索引元数据，制造不兼容。
    (old / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "3", "embedding_model": "other-model",
                    "vector_dimension": 64, "chunk_version": "2"}), encoding="utf-8")
    with pytest.raises(ValueError, match="incompatible"):
        maintenance().migrate_reusing_vectors(
            path, old, tmp_path / "staging-incompatible",
            embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)


def test_migrate_reuse_refuses_existing_staging_and_overlap(migrate_source, tmp_path):
    path, old, *_ = migrate_source
    existing = tmp_path / "staging-existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        maintenance().migrate_reusing_vectors(
            path, old, existing, embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)
    with pytest.raises(ValueError, match="overlap"):
        maintenance().migrate_reusing_vectors(
            path, old, old / "staging", embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)


def test_migrate_reuse_requires_stopped(migrate_source, tmp_path):
    path, old, *_ = migrate_source
    with pytest.raises(ValueError, match="stopped"):
        maintenance().migrate_reusing_vectors(
            path, old, tmp_path / "staging-not-stopped",
            embedding_model="local-hash-v1", vector_dimension=64, app_stopped=False)
    assert not (tmp_path / "staging-not-stopped").exists()


def _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id):
    return {
        "candidate": [{"id": f"{candidate_id}-0", "candidate_id": candidate_id,
                        "revision_id": revision_id, "keyword_text": "Python 后端",
                        "vector": [0.1] * 64}],
        "jd": [{"id": f"{jd_id}-0", "candidate_id": jd_id,
                 "revision_id": jd_revision_id, "keyword_text": "后端工程师",
                 "vector": [0.2] * 64}],
    }


def _validate(migrate_source, staged):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    source = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    return maintenance()._validate_staging(path, source, staged, 64)


def _assert_failure(migrate_source, staged, keyword):
    failures, checks = _validate(migrate_source, staged)
    assert any(keyword in f for f in failures), failures
    return failures


def test_validate_missing_candidate_chunk(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["candidate"] = []
    _assert_failure(migrate_source, staged, "candidate")


def test_validate_missing_jd_chunk(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["jd"] = []
    _assert_failure(migrate_source, staged, "jd")


def test_validate_modified_candidate_vector(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["candidate"][0]["vector"] = [0.9] * 64
    _assert_failure(migrate_source, staged, "candidate")


def test_validate_modified_jd_vector(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["jd"][0]["vector"] = [0.9] * 64
    _assert_failure(migrate_source, staged, "jd")


def test_validate_wrong_vector_dimension(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["candidate"][0]["vector"] = [0.1] * 63
    _assert_failure(migrate_source, staged, "维度")


def test_validate_entity_count_mismatch_db(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["candidate"][0]["candidate_id"] = "not-in-db"
    _assert_failure(migrate_source, staged, "candidate")


def test_validate_content_or_chunk_id_mismatch(migrate_source):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staged = _valid_rows(candidate_id, revision_id, jd_id, jd_revision_id)
    staged["candidate"][0]["keyword_text"] = "被篡改的内容"
    _assert_failure(migrate_source, staged, "candidate")


def _source_checksum(root):
    candidate = maintenance()._vector_checksum(maintenance()._read_index_rows(root))
    jd = maintenance()._vector_checksum(maintenance()._read_index_rows(root / "jobs"))
    return candidate + jd


def test_migrate_reuse_gates_on_entity_count_mismatch(migrate_source, tmp_path):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    before = _source_checksum(old)
    # 向数据库新增一个 READY current 候选，但源索引没有它的 chunk。
    engine = create_engine_for(path)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        blob = Blob(content_sha256="e" * 64, suffix=".pdf", size_bytes=1, storage_path="unused-extra")
        candidate = Candidate(display_name="新增候选")
        session.add(ResumeRevision(document=ResumeDocument(candidate=candidate), blob=blob,
            original_filename="n.pdf", content_sha256="e" * 64, status="READY", is_current=True,
            parsed_data={"direction_profile": _direction_json("BACKEND")}))
    engine.dispose()
    staging = tmp_path / "staging-mismatch"
    with pytest.raises(ValueError, match="validation failed"):
        maintenance().migrate_reusing_vectors(
            path, old, staging, embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)
    # manifest 为 FAILED_VALIDATION，且不得出现 READY。
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED_VALIDATION"
    assert manifest["status"] != "READY_FOR_OFFLINE_SWITCH"
    assert manifest["failures"]
    # staging 保留用于诊断；源索引 checksum 不变。
    assert staging.exists()
    assert _source_checksum(old) == before


def test_validate_index_cli_fails_on_missing_chunk(migrate_source, tmp_path):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staging = tmp_path / "staging-valid"
    maintenance().migrate_reusing_vectors(
        path, old, staging, embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)
    before = _source_checksum(old)
    # 删除 staging 的 candidate chunk，制造校验失败。
    import lancedb
    db = lancedb.connect(str(staging))
    table = db.open_table("candidate_chunks")
    table.delete(f"id = '{candidate_id}-0'")
    result = maintenance().validate_index(
        path, old, staging, embedding_model="local-hash-v1", vector_dimension=64)
    assert result["status"] == "FAILED_VALIDATION"
    assert result["failures"]
    from kerui_recruit.search.maintenance import main as maintenance_main
    rc = maintenance_main(["validate", "--database", str(path), "--index-root", str(old),
                           "--model", "local-hash-v1", "--dimension", "64", "--staging-root", str(staging)])
    assert rc == 1
    assert _source_checksum(old) == before


def test_db_projected_ids_open_jds_only(tmp_path):
    path = tmp_path / "jdcount.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        for i in range(48):
            status = "OPEN" if i < 15 else "PAUSED"
            jd = Jd(title=f"JD-{i}", company="C", status=status)
            session.add(jd)
            session.flush()
            session.add(JdRevision(jd=jd, source_text="x", status="READY", is_current=True))
    engine.dispose()
    ids = maintenance()._db_projected_ids(path)
    assert len(ids["jd"]) == 15
    assert len(ids["candidate"]) == 0


def test_db_projected_ids_excludes_closed_and_archived_jds(tmp_path):
    path = tmp_path / "jdexclude.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        for status in ("OPEN", "FILLED", "ARCHIVED", "CANCELLED", "DRAFT"):
            jd = Jd(title=f"jd-{status}", company="C", status=status)
            session.add(jd)
            session.flush()
            session.add(JdRevision(jd=jd, source_text="x", status="READY", is_current=True))
    engine.dispose()
    ids = maintenance()._db_projected_ids(path)
    assert len(ids["jd"]) == 1  # 只有 OPEN 投影


def test_db_projected_ids_excludes_archived_and_pending_candidates(tmp_path):
    path = tmp_path / "candexclude.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        blob = Blob(content_sha256="a" * 64, suffix=".pdf", size_bytes=1, storage_path="unused-1")
        for status in ("AVAILABLE", "ARCHIVED", "PENDING_REVIEW", "ON_HOLD"):
            candidate = Candidate(display_name=status, status=status)
            session.add(ResumeRevision(
                document=ResumeDocument(candidate=candidate), blob=blob,
                original_filename="r.pdf", content_sha256="a" * 64,
                status="READY", is_current=True,
                parsed_data={"direction_profile": _direction_json("BACKEND")}))
    engine.dispose()
    ids = maintenance()._db_projected_ids(path)
    # AVAILABLE + ON_HOLD 投影；ARCHIVED + PENDING_REVIEW 不投影。
    assert len(ids["candidate"]) == 2


def test_db_projected_ids_multiple_current_revisions_one_entity(tmp_path):
    path = tmp_path / "multi-revision.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    factory = sessionmaker(engine)
    with factory() as session, session.begin():
        blob = Blob(content_sha256="b" * 64, suffix=".pdf", size_bytes=1, storage_path="unused-2")
        candidate = Candidate(display_name="多版本", status="AVAILABLE")
        for i in range(2):
            session.add(ResumeRevision(
                document=ResumeDocument(candidate=candidate), blob=blob,
                original_filename=f"r{i}.pdf", content_sha256="b" * 64,
                status="READY", is_current=True,
                parsed_data={"direction_profile": _direction_json("BACKEND")}))
    engine.dispose()
    ids = maintenance()._db_projected_ids(path)
    assert len(ids["candidate"]) == 1


def test_validate_empty_index_passes(tmp_path):
    path = tmp_path / "empty.sqlite3"
    engine = create_engine_for(path)
    migrate(engine)
    engine.dispose()
    source = tmp_path / "empty-source"
    staging = tmp_path / "empty-staging"
    for root in (source, staging):
        LanceDBSearchIndex(root, vector_dimension=64, embedding_model="local-hash-v1")
        JdSearchIndex(root / "jobs", vector_dimension=64, embedding_model="local-hash-v1")
    result = maintenance().validate_index(
        path, source, staging, embedding_model="local-hash-v1", vector_dimension=64)
    assert result["status"] == "VALIDATED"


def test_validate_staging_metadata_mismatch_fails(migrate_source, tmp_path):
    path, old, candidate_id, revision_id, jd_id, jd_revision_id = migrate_source
    staging = tmp_path / "staging"
    maintenance().migrate_reusing_vectors(
        path, old, staging, embedding_model="local-hash-v1", vector_dimension=64, app_stopped=True)
    (staging / "candidate-index-metadata.json").write_text(
        json.dumps({"schema_version": "3", "embedding_model": "other-model",
                    "vector_dimension": 64, "chunk_version": "2"}), encoding="utf-8")
    result = maintenance().validate_index(
        path, old, staging, embedding_model="local-hash-v1", vector_dimension=64)
    assert result["status"] == "FAILED_VALIDATION"
    assert any("metadata" in f for f in result["failures"])


def _make_legacy_table(root: Path, metadata: dict, dim: int = 64) -> None:
    import lancedb as ldb
    import pyarrow as pa
    probe = LanceDBSearchIndex(root / "probe", vector_dimension=dim)
    full = probe._schema()
    drop = {"kind", "sequence", "evidence_path", "specializations"}
    schema8 = pa.schema([f for f in full if f.name not in drop])
    db = ldb.connect(str(root))
    db.create_table("candidate_chunks", schema=schema8)
    (root / "candidate-index-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def test_upgrade_legacy_indexes_prechecks_both_before_writing(tmp_path):
    """candidate 可升级、jd 不可升级时，预检必须整体失败且不修改任何索引。"""
    index_root = tmp_path / "index-root"
    candidate_root = index_root
    jd_root = index_root / "jobs"
    candidate_root.mkdir(parents=True)
    jd_root.mkdir(parents=True)

    _make_legacy_table(candidate_root, {"schema_version": "8", "embedding_model": "local-hash-v1",
                                        "vector_dimension": 64, "chunk_version": "6"})
    _make_legacy_table(jd_root, {"schema_version": "7", "embedding_model": "local-hash-v1",
                                 "vector_dimension": 64, "chunk_version": "5"})

    result = maintenance().upgrade_legacy_indexes(index_root, embedding_model="local-hash-v1", dimension=64)
    assert result["status"] == "failed"
    assert "jd" in result["errors"]
    # candidate 虽可升级，但因 jd 预检失败，未被修改（仍为 8）。
    assert json.loads((candidate_root / "candidate-index-metadata.json").read_text())["schema_version"] == "8"

