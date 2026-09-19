"""Offline, staged index rebuilding. The active database/index are never written.

Run with ``python -m kerui_recruit.search.rebuild_maintenance --help``.
Default operation is a read-only diagnosis; no provider is instantiated.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct

import httpx
import lancedb
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.core.settings_store import SettingsStore
from kerui_recruit.db.migrate import SCHEMA_VERSION
from kerui_recruit.db.models import Candidate, IndexSyncRecord, Jd
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.providers.local import LocalHashEmbeddingProvider
from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lancedb_index import (
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)
from kerui_recruit.search.sync import IndexSyncService, enqueue_sync


def _readonly(database: Path) -> sqlite3.Connection:
    path = database.expanduser().resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _metadata(model: str, dimension: int) -> dict:
    if dimension < 1:
        raise ValueError("dimension must be positive")
    return {"schema_version": INDEX_SCHEMA_VERSION, "embedding_model": model,
            "vector_dimension": dimension, "chunk_version": INDEX_CHUNK_VERSION}


def _execution_supported(model: str, dimension: int) -> bool:
    if model == "local-hash-v1":
        return dimension == 64
    return dimension == SiliconFlowEmbeddingProvider.dimension


def _siliconflow_api_key(database: Path) -> str | None:
    """Resolve the SiliconFlow API key from the environment or stored settings.

    Stored keys live in ``<data-root>/config/settings.json`` and are encrypted
    with the AES key in ``<data-root>/config/encryption.key``. Never log either.
    """
    env_key = os.environ.get("SILICONFLOW_API_KEY")
    if env_key:
        return env_key
    config_dir = database.parent.parent / "config"
    data = SettingsStore(config_dir / "settings.json").load()
    value = data.get("siliconflow_api_key") if data else None
    if not value:
        return None
    key_path = config_dir / "encryption.key"
    if not key_path.is_file():
        return None
    try:
        return EncryptionService(key_path=str(key_path)).decrypt(value)
    except Exception:
        return None


def _build_embedding_provider(model: str, dimension: int, database: Path):
    """Return ``(provider, http_client)`` for the requested embedding model.

    The local provider owns no network client. The remote provider shares an
    ``httpx.AsyncClient`` that the caller must close after the build finishes.
    """
    if model == "local-hash-v1":
        if dimension != 64:
            raise ValueError("local-hash-v1 requires vector dimension 64")
        return LocalHashEmbeddingProvider(dimension=dimension), None
    if dimension != SiliconFlowEmbeddingProvider.dimension:
        raise ValueError(
            f"Remote embedding dimension must be {SiliconFlowEmbeddingProvider.dimension}, got {dimension}"
        )
    api_key = _siliconflow_api_key(database)
    if not api_key:
        raise ValueError(
            "SiliconFlow API key is required for remote index rebuild; "
            "set SILICONFLOW_API_KEY or configure it in the application settings"
        )
    client = httpx.AsyncClient()
    provider = SiliconFlowEmbeddingProvider(
        api_key=api_key,
        client=client,
        base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        model=model,
    )
    return provider, client


def _inspect_metadata(root: Path, expected: dict) -> dict:
    # Do not instantiate LanceDBSearchIndex here: its constructor creates paths.
    path = root / "candidate-index-metadata.json"
    if not path.is_file():
        return {"metadata": None, "compatibility": "missing-metadata-or-empty"}
    try:
        actual = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"metadata": None, "compatibility": "invalid-metadata"}
    return {"metadata": actual, "compatibility": (
        "metadata-matches-physical-schema-not-checked" if actual == expected else "rebuild-required")}


def diagnose(database: Path, index_root: Path, *, embedding_model: str, dimension: int) -> dict:
    expected = _metadata(embedding_model, dimension)
    with closing(_readonly(database)) as source:
        version = source.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        counts = {table: source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("candidate", "jd")}
    return {"mode": "dry-run", "database_schema_version": version,
            "database_schema_supported": version == SCHEMA_VERSION,
            "counts": counts, "target_metadata": expected,
            "candidate_index": _inspect_metadata(index_root, expected),
            "jd_index": _inspect_metadata(index_root / "jobs", expected),
            "execution_supported": _execution_supported(embedding_model, dimension),
            "warning": "Metadata comparison only; active index was not opened or modified. "
                       "Execution supports local-hash-v1/64 and SiliconFlow-served remote embeddings; "
                       "a remote rebuild reads the SiliconFlow API key from the environment or stored settings."}


def _manifest(output: Path, data: dict) -> None:
    temporary = output / "manifest.json.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output / "manifest.json")


def _projected_ids(index: LanceDBSearchIndex) -> set[str]:
    if not index.warmup():
        return set()
    table = index.database.open_table(index.table_name)
    return {row["candidate_id"] for row in
            table.search(None).select(["candidate_id"]).limit(None).to_list()}


async def build_offline(database: Path, index_root: Path, output: Path, *,
                        app_stopped: bool, embedding_provider=None,
                        embedding_model: str = "local-hash-v1", dimension: int = 64) -> dict:
    """Build a separate projection; never promote or migrate source.

    ``embedding_provider`` is dependency injection for verification. When it is
    omitted the local provider is used; a remote model must be supplied with its
    own provider (and HTTP client) by the caller, matching ``embedding_model``
    and ``dimension``.
    """
    if not app_stopped:
        raise ValueError("Application must be stopped through build and manual switch")
    if embedding_provider is None and embedding_model != "local-hash-v1":
        raise ValueError("A remote embedding model requires an injected embedding provider")
    database = database.expanduser().resolve(strict=True)
    index_root, output = index_root.resolve(), output.resolve()
    if (output == index_root or output.is_relative_to(index_root) or
            index_root.is_relative_to(output) or database.is_relative_to(output)):
        raise ValueError("Output must not overlap the active database or index paths")
    if output.exists():
        raise ValueError("Output already exists; select a new staging directory")
    diagnostic = diagnose(database, index_root, embedding_model=embedding_model, dimension=dimension)
    if not diagnostic["database_schema_supported"]:
        raise ValueError("Source SQLite schema requires the application's separate migration workflow")
    output.mkdir(parents=True, exist_ok=False)
    state = {"status": "BUILDING", "source_database": str(database), "active_index": str(index_root),
             "started_at": datetime.now(timezone.utc).isoformat(),
             "metadata": diagnostic["target_metadata"], "automatic_switch": False}
    _manifest(output, state)
    engine = None
    try:
        with closing(_readonly(database)) as source:
            version_before = source.execute("PRAGMA data_version").fetchone()[0]
            snapshot_path = output / "source-snapshot.sqlite3"
            with closing(sqlite3.connect(snapshot_path)) as snapshot:
                source.backup(snapshot)
            engine = create_engine_for(snapshot_path)
            factory = sessionmaker(engine, expire_on_commit=False)
            index = LanceDBSearchIndex(output / "search", vector_dimension=dimension,
                                       embedding_model=embedding_model)
            jobs = JdSearchIndex(output / "search" / "jobs", vector_dimension=dimension,
                                 embedding_model=embedding_model)
            sync = IndexSyncService(session_factory=factory, index=index, jd_index=jobs,
                                    embedding_provider=embedding_provider or LocalHashEmbeddingProvider(dimension=dimension))
            with factory() as session, session.begin():
                entities = [("candidate", value) for value in session.scalars(select(Candidate.id))]
                entities.extend(("jd", value) for value in session.scalars(select(Jd.id)))
                # Only the isolated snapshot's queue is rewritten. Source tasks
                # and outbox acknowledgements must never be copied back.
                session.execute(delete(IndexSyncRecord))
                for kind, entity_id in entities:
                    enqueue_sync(session, kind, entity_id)
            expected_ids = {"candidate": set(), "jd": set()}
            expected_chunks = {"candidate": 0, "jd": 0}
            for kind, entity_id in entities:
                snapshot = sync._snapshot(kind, entity_id)
                if snapshot is not None:
                    expected_ids[kind].add(entity_id)
                    expected_chunks[kind] += len(snapshot["documents"])
            stalled_cycles = 0
            while sync.status()["pending"]:
                completed = await sync.run_once(batch_size=25, force=True)
                status = sync.status()
                # Remote providers can fail transiently (429/timeout). With
                # force=True those entities are retried immediately; only abort
                # after consecutive no-progress cycles so a single blip does not
                # discard the whole staged build.
                if status["pending"] and not completed:
                    stalled_cycles += 1
                    if stalled_cycles >= 5:
                        raise RuntimeError("Staged index build failed; source and old index were not replaced")
                else:
                    stalled_cycles = 0
            validated = {}
            for kind, target in (("candidate", index), ("jd", jobs.index)):
                target.optimize_pending()
                if not target.is_compatible():
                    raise RuntimeError("Staged index metadata validation failed")
                if target.warmup() != expected_chunks[kind] or _projected_ids(target) != expected_ids[kind]:
                    raise RuntimeError("Staged index entity/chunk validation failed")
                validated[f"{kind}_entities"] = len(expected_ids[kind])
                validated[f"{kind}_chunks"] = expected_chunks[kind]
            if source.execute("PRAGMA data_version").fetchone()[0] != version_before:
                raise RuntimeError("Source database changed during build; staged index must not be switched")
        state.update(status="READY_FOR_OFFLINE_SWITCH", validated=validated,
                     completed_at=datetime.now(timezone.utc).isoformat(),
                     instruction="Keep application stopped. Preserve the complete old search directory; "
                                 "move staged search (including jobs) into its place, then restart with the "
                                 "matching embedding model configured. Never restore the snapshot over the live database.")
        _manifest(output, state)
        return state
    except BaseException as error:
        # Only internal maintenance errors reach this point; provider payloads
        # and resume bodies are already suppressed by the sync layer.
        state.update(status="FAILED", error_type=type(error).__name__, error_message=str(error))
        _manifest(output, state)
        raise
    finally:
        if engine is not None:
            engine.dispose()


def _read_index_rows(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    db = lancedb.connect(str(root))
    if "candidate_chunks" not in db.list_tables().tables:
        return []
    table = db.open_table("candidate_chunks")
    return table.search(None).limit(None).to_list()


def _chunk_from_row(row: dict) -> SearchChunk:
    return SearchChunk(
        id=row["id"],
        candidate_id=row["candidate_id"],
        revision_id=row["revision_id"],
        content=row.get("keyword_text") or "",
        vector=tuple(float(v) for v in (row.get("vector") or [])),
        total_years=row.get("total_years"),
        highest_degree=row.get("highest_degree"),
        location=row.get("location"),
        candidate_status=row.get("candidate_status", "AVAILABLE"),
        qs_rank=row.get("qs_rank"),
        school_level=row.get("school_level"),
        preferred_location=row.get("preferred_location"),
        preferred_locations=tuple(row.get("preferred_locations") or ()),
        keyword_text=row.get("keyword_text"),
        keyword_index_text=row.get("keyword_index_text"),
        vector_text=row.get("vector_text"),
        name_terms=tuple(row.get("name_terms") or ()),
        school_terms=tuple(row.get("school_terms") or ()),
        company_terms=tuple(row.get("company_terms") or ()),
        title_terms=tuple(row.get("title_terms") or ()),
        location_terms=tuple(row.get("location_terms") or ()),
        skills=tuple(row.get("skills") or ()),
        age=row.get("age"),
        school_tags=tuple(row.get("school_tags") or ()),
    )


def _vector_checksum(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: str(r.get("id") or "")):
        for value in (row.get("vector") or []):
            digest.update(struct.pack(">f", float(value)))
    return digest.hexdigest()


def _sample_compare(source: list[dict], staged: list[dict], sample: int) -> dict:
    staged_by_id = {str(r.get("id") or ""): r for r in staged}
    compared = 0
    mismatches = 0
    for row in sorted(source, key=lambda r: str(r.get("id") or ""))[:sample]:
        target = staged_by_id.get(str(row.get("id") or ""))
        if target is None:
            mismatches += 1
            continue
        a = [float(v) for v in (row.get("vector") or [])]
        b = [float(v) for v in (target.get("vector") or [])]
        if len(a) != len(b) or any(abs(x - y) > 1e-5 for x, y in zip(a, b)):
            mismatches += 1
        compared += 1
    return {"sampled": compared, "mismatches": mismatches,
            "all_match": mismatches == 0}


def _db_projected_ids(database: Path) -> dict[str, set[str]]:
    """计算数据库“应该投影”的实体 ID 集合，资格与 IndexSyncService._snapshot 完全一致。"""
    with closing(_readonly(database)) as source:
        candidate_rows = source.execute(
            "SELECT DISTINCT d.candidate_id FROM resume_revision r "
            "JOIN resume_document d ON d.id = r.document_id "
            "JOIN candidate c ON c.id = d.candidate_id "
            "WHERE r.is_current = 1 AND r.status = 'READY' "
            "AND c.deleted_at IS NULL AND c.status NOT IN ('ARCHIVED', 'PENDING_REVIEW')"
        ).fetchall()
        jd_rows = source.execute(
            "SELECT DISTINCT j.id FROM jd_revision r "
            "JOIN jd j ON j.id = r.jd_id "
            "WHERE r.is_current = 1 AND r.status = 'READY' "
            "AND j.deleted_at IS NULL AND j.status = 'OPEN'"
        ).fetchall()
    return {
        "candidate": {str(row[0]) for row in candidate_rows},
        "jd": {str(row[0]) for row in jd_rows},
    }


def _validate_staging(database: Path, source_rows: dict[str, list[dict]],
                      staged_rows: dict[str, list[dict]],
                      vector_dimension: int) -> tuple[list[str], dict]:
    """校验 staging 是否完整等价于源索引。返回 (失败项, 校验明细)。"""
    failures: list[str] = []
    checks: dict = {}
    db_ids = _db_projected_ids(database)

    for kind in ("candidate", "jd"):
        src = source_rows[kind]
        staged = staged_rows[kind]
        source_entities = {str(r.get("candidate_id") or "") for r in src}
        staged_entities = {str(r.get("candidate_id") or "") for r in staged}
        checks[f"{kind}_entities"] = len(staged_entities)
        checks[f"{kind}_chunks"] = len(staged)

        chunks_match = len(staged) == len(src)
        checks[f"{kind}_chunks_match"] = chunks_match
        if not chunks_match:
            failures.append(f"{kind} chunk 数不一致")

        entities_match_source = staged_entities == source_entities
        checks[f"{kind}_entities_match_source"] = entities_match_source
        if not entities_match_source:
            failures.append(f"{kind} entity 集合与源索引不一致")

        entities_match_db = staged_entities == db_ids[kind]
        checks[f"{kind}_entities_match_db"] = entities_match_db
        if not entities_match_db:
            failures.append(f"{kind} entity 集合与数据库应投影集合不一致")

        bad_dimension = [r for r in staged if len(r.get("vector") or []) != vector_dimension]
        checks[f"{kind}_vector_dimension_ok"] = not bad_dimension
        if bad_dimension:
            failures.append(f"{kind} 存在向量维度错误")

        checksum_match = _vector_checksum(src) == _vector_checksum(staged)
        checks[f"{kind}_vector_checksum_match"] = checksum_match
        if not checksum_match:
            failures.append(f"{kind} 向量 checksum 不一致")

        sampled = _sample_compare(src, staged, min(20, len(src)))
        checks[f"{kind}_sampled_vector_compare"] = sampled
        if not sampled["all_match"]:
            failures.append(f"{kind} 抽样逐元素比较不一致")

        source_by_id = {str(r.get("id") or ""): r for r in src}
        mismatched = 0
        for row in staged:
            ref = source_by_id.get(str(row.get("id") or ""))
            if ref is None or (
                str(ref.get("candidate_id") or "") != str(row.get("candidate_id") or "")
                or str(ref.get("revision_id") or "") != str(row.get("revision_id") or "")
                or ref.get("keyword_text") != row.get("keyword_text")
                or ref.get("keyword_index_text") != row.get("keyword_index_text")
            ):
                mismatched += 1
        checks[f"{kind}_chunk_correspondence"] = mismatched == 0
        if mismatched:
            failures.append(f"{kind} chunk 对应关系不一致")

    return failures, checks


def _verify_metadata(expected: dict, roots: dict[str, Path]) -> tuple[list[str], dict]:
    """真实读取并校验各索引的 metadata（embedding model / dimension / chunk version）。"""
    failures: list[str] = []
    checks: dict = {}
    for label, root in roots.items():
        inspected = _inspect_metadata(root, expected)
        checks[f"{label}_metadata"] = inspected
        # 空索引（无 metadata 文件）视为合法；只有不匹配或损坏才失败。
        if inspected["compatibility"] not in (
            "metadata-matches-physical-schema-not-checked", "missing-metadata-or-empty"):
            failures.append(f"{label} metadata 不兼容: {inspected['compatibility']}")
    return failures, checks


def migrate_reusing_vectors(
    database: Path,
    source_index_root: Path,
    staging_index_root: Path,
    *,
    embedding_model: str,
    vector_dimension: int,
    app_stopped: bool = False,
) -> dict:
    """复用旧索引向量，仅补齐/刷新方向元数据，写入独立 staging 目录。"""
    if not app_stopped:
        raise ValueError("Application must be stopped through migration and manual switch")
    database = database.expanduser().resolve(strict=True)
    source_index_root = source_index_root.expanduser().resolve(strict=True)
    staging_index_root = staging_index_root.expanduser().resolve()
    if (staging_index_root == source_index_root
            or staging_index_root.is_relative_to(source_index_root)
            or source_index_root.is_relative_to(staging_index_root)):
        raise ValueError("Staging path must not overlap the source index")
    if staging_index_root.exists():
        raise ValueError("Staging directory already exists")

    expected = _metadata(embedding_model, vector_dimension)
    for kind, root in (("candidate", source_index_root), ("jd", source_index_root / "jobs")):
        inspected = _inspect_metadata(root, expected)
        if inspected["compatibility"] != "metadata-matches-physical-schema-not-checked":
            raise ValueError(f"{kind} source index metadata incompatible: {inspected['compatibility']}")

    candidate_rows = _read_index_rows(source_index_root)
    jd_rows = _read_index_rows(source_index_root / "jobs")

    staging_index_root.mkdir(parents=True, exist_ok=False)
    candidate_index = LanceDBSearchIndex(staging_index_root, vector_dimension=vector_dimension,
                                          embedding_model=embedding_model)
    jobs = JdSearchIndex(staging_index_root / "jobs", vector_dimension=vector_dimension,
                          embedding_model=embedding_model)

    candidate_index.upsert([_chunk_from_row(r) for r in candidate_rows])
    candidate_index.optimize_pending()
    jobs.index.upsert([_chunk_from_row(r) for r in jd_rows])
    jobs.index.optimize_pending()

    staged_candidate_rows = _read_index_rows(staging_index_root)
    staged_jd_rows = _read_index_rows(staging_index_root / "jobs")

    failures, validated = _validate_staging(
        database,
        {"candidate": candidate_rows, "jd": jd_rows},
        {"candidate": staged_candidate_rows, "jd": staged_jd_rows},
        vector_dimension,
    )
    validated["sampled_vector_compare"] = validated["candidate_sampled_vector_compare"]

    status = "READY_FOR_OFFLINE_SWITCH" if not failures else "FAILED_VALIDATION"
    manifest = {
        "status": status,
        "source_database": str(database),
        "source_index": str(source_index_root),
        "staging_index": str(staging_index_root),
        "embedding_model": embedding_model,
        "vector_dimension": vector_dimension,
        "reused_vectors": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "validated": validated,
        "failures": failures,
        "instruction": "Keep application stopped. Preserve the old search directory; "
                       "move the staged search directory into place, then restart.",
    }
    _manifest(staging_index_root, manifest)
    if failures:
        raise ValueError(f"Staged index validation failed: {', '.join(failures)}")
    return manifest


def validate_index(database: Path, source_index_root: Path, staging_index_root: Path,
                   *, embedding_model: str, vector_dimension: int) -> dict:
    candidate_rows = _read_index_rows(source_index_root)
    jd_rows = _read_index_rows(source_index_root / "jobs")
    staged_candidate_rows = _read_index_rows(staging_index_root)
    staged_jd_rows = _read_index_rows(staging_index_root / "jobs")
    failures, checks = _validate_staging(
        database,
        {"candidate": candidate_rows, "jd": jd_rows},
        {"candidate": staged_candidate_rows, "jd": staged_jd_rows},
        vector_dimension,
    )
    expected = _metadata(embedding_model, vector_dimension)
    metadata_failures, metadata_checks = _verify_metadata(expected, {
        "source_candidate": source_index_root,
        "source_jd": source_index_root / "jobs",
        "staging_candidate": staging_index_root,
        "staging_jd": staging_index_root / "jobs",
    })
    failures.extend(metadata_failures)
    checks.update(metadata_checks)
    checks["sampled_vector_compare"] = checks["candidate_sampled_vector_compare"]
    checks["target_metadata"] = expected
    checks["failures"] = failures
    checks["status"] = "VALIDATED" if not failures else "FAILED_VALIDATION"
    return checks


def upgrade_legacy_indexes(index_root: Path, *, embedding_model: str, dimension: int) -> dict:
    """把旧 schema 8 / chunk 6 索引就地升级为可写（补列 + 更新 metadata）。

    只用于**隔离副本**的显式迁移，绝不在运行中的真实数据上调用；回退依赖调用方
    保留原始归档副本。**先对 candidate + jd 两个索引做只读预检**，任一不合格即
    不修改任何索引并返回失败；全部合格才逐个迁移。
    """
    roots = {"candidate": index_root, "jd": index_root / "jobs"}
    instances = {
        label: LanceDBSearchIndex(root, vector_dimension=dimension, embedding_model=embedding_model)
        for label, root in roots.items()
    }
    # 预检：任一不合格即整体失败，不写入任何索引。
    errors = {label: inst.legacy_upgrade_error() for label, inst in instances.items()}
    if any(errors.values()):
        return {
            "status": "failed",
            "errors": {label: err for label, err in errors.items() if err},
            "indexes": {label: {"upgraded": False, "compatible": inst.is_compatible()}
                        for label, inst in instances.items()},
        }
    results = {}
    for label, inst in instances.items():
        changed = inst.upgrade_legacy_schema()
        results[label] = {"upgraded": changed, "compatible": inst.is_compatible(), "error": None}
    return {"status": "upgraded", "indexes": results}


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--index-root", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimension", required=True, type=int)
    parser.add_argument("--execute", action="store_true", help="Build separate staging output (default: diagnosis only)")
    parser.add_argument("--upgrade-legacy", action="store_true", help="In-place upgrade legacy schema 8/chunk 6 indexes (isolated copy only)")
    parser.add_argument("--app-stopped", action="store_true", help="Confirm application remains closed until manual switch")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    try:
        if args.upgrade_legacy:
            if not args.app_stopped:
                print(json.dumps({"status": "FAILED", "error_type": "ValueError",
                                  "instruction": "--upgrade-legacy requires --app-stopped; never migrate a live data directory."}))
                return 1
            result = upgrade_legacy_indexes(args.index_root, embedding_model=args.model, dimension=args.dimension)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("status") == "upgraded" else 1
        elif args.execute:
            if args.output is None:
                raise ValueError("--output is required for execution")
            provider, http_client = _build_embedding_provider(args.model, args.dimension, args.database)

            async def _run() -> dict:
                # Close the shared client in the same event loop that ran the
                # build; closing it from a fresh loop raises and would wrongly
                # mask a successful build as a failure.
                try:
                    return await build_offline(
                        args.database, args.index_root, args.output, app_stopped=args.app_stopped,
                        embedding_provider=provider, embedding_model=args.model, dimension=args.dimension)
                finally:
                    if http_client is not None:
                        await http_client.aclose()

            result = asyncio.run(_run())
        else:
            result = diagnose(args.database, args.index_root, embedding_model=args.model, dimension=args.dimension)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        # Do not print provider payloads, SQL parameters, resume bodies or secrets.
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "instruction": "No index was switched. Check inputs, shutdown status and staging manifest."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
