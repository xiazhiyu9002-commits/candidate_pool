"""方案 1 检索评测：生产口径前缀（base）vs AI ≤15 字前缀（ai15）。

同一冻结快照上构造两组子切片，各自建隔离索引，用真实 BGE-M3 + 真实
BGE-reranker-v2-m3 跑向量模式与混合模式，落盘各变体 top100（alias）。

- base ：``build_child_documents(data)`` 原样（= 当前生产口径，前缀回退 ``ai_profile_summary[:60]``）；
- ai15 ：``build_child_documents({**data, "ai_profile_compact": compact})``（单变量，不改生产代码）。

写隔离目录 ``.tmp-prefix-eval/``，不碰 ``.dev-data``；密钥只用于请求头，不打印。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import httpx
import lancedb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
LIMIT = 100
BATCH = 25
MODES = ("vector", "hybrid")


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def run_revisions() -> dict[str, dict]:
    conn = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    rows = conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ).fetchall()
    conn.close()
    return {rid: (json.loads(raw) if isinstance(raw, str) else raw or {}) for rid, raw in rows}


def as_paths(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(x) for x in (value or ()) if x)


def to_chunk(row: dict) -> "object":
    from kerui_recruit.search.contracts import SearchChunk

    return SearchChunk(
        id=row["id"], candidate_id=row["candidate_id"], revision_id=row["revision_id"],
        content=row.get("keyword_text") or row.get("content") or "",
        vector=tuple(float(x) for x in row["vector"]),
        total_years=row.get("total_years"), highest_degree=row.get("highest_degree"),
        location=row.get("location"), candidate_status=row.get("candidate_status", "AVAILABLE"),
        qs_rank=row.get("qs_rank"), school_level=row.get("school_level"),
        preferred_location=row.get("preferred_location"),
        preferred_locations=tuple(row.get("preferred_locations") or ()),
        keyword_text=row.get("keyword_text"), keyword_index_text=row.get("keyword_index_text"),
        vector_text=row.get("vector_text"), body_index_text=row.get("body_index_text") or "",
        chunk_type=row.get("chunk_type", "parent"), parent_id=row.get("parent_id"),
        kind=row.get("kind") or "parent", sequence=row.get("sequence"), evidence_path=(),
        name_terms=tuple(row.get("name_terms") or ()), school_terms=tuple(row.get("school_terms") or ()),
        company_terms=tuple(row.get("company_terms") or ()), title_terms=tuple(row.get("title_terms") or ()),
        location_terms=tuple(row.get("location_terms") or ()), skills=tuple(row.get("skills") or ()),
        age=row.get("age"), school_tags=tuple(row.get("school_tags") or ()),
        direction=row.get("direction"), school_region=row.get("school_region"),
        specializations=tuple(row.get("specializations") or ()),
    )


def child_chunk(parent: dict, child: dict, vector: list[float]) -> "object":
    from kerui_recruit.search.contracts import SearchChunk

    return SearchChunk(
        id=f"{parent['revision_id']}:{child['kind']}:{child.get('sequence')}",
        candidate_id=parent["candidate_id"], revision_id=parent["revision_id"],
        content=child["vector_text"], vector=tuple(float(x) for x in vector),
        total_years=parent.get("total_years"), highest_degree=parent.get("highest_degree"),
        location=parent.get("location"), candidate_status=parent.get("candidate_status", "AVAILABLE"),
        qs_rank=parent.get("qs_rank"), school_level=parent.get("school_level"),
        preferred_location=parent.get("preferred_location"),
        preferred_locations=tuple(parent.get("preferred_locations") or ()),
        keyword_text=child["vector_text"], keyword_index_text=child.get("keyword_index_text") or "",
        vector_text=child["vector_text"], body_index_text="",
        chunk_type="child", parent_id=parent["revision_id"],
        kind=child["kind"], sequence=child.get("sequence"), evidence_path=as_paths(child.get("evidence_path")),
        name_terms=tuple(parent.get("name_terms") or ()), school_terms=tuple(parent.get("school_terms") or ()),
        company_terms=tuple(parent.get("company_terms") or ()), title_terms=tuple(parent.get("title_terms") or ()),
        location_terms=tuple(parent.get("location_terms") or ()), skills=tuple(parent.get("skills") or ()),
        age=parent.get("age"), school_tags=tuple(parent.get("school_tags") or ()),
        direction=parent.get("direction"), school_region=parent.get("school_region"),
        specializations=tuple(parent.get("specializations") or ()),
    )


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider, SiliconFlowRerankerProvider
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.documents import build_child_documents
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.service import HybridSearchService

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    intents = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    queries = {qid: item["intent"] for qid, item in intents.items() if qid <= "Q24"}
    prefixes = json.loads((EVAL_DIR / "prefix_ai15.json").read_text(encoding="utf-8"))
    revisions = run_revisions()
    print(f"queries={len(queries)} revisions={len(revisions)} prefixes={len(prefixes)}", flush=True)

    db = lancedb.connect(str(SNAPSHOT / "search"))
    table = db.open_table("candidate_chunks")
    rows = table.search(None).limit(None).to_list()
    parent_rows = [r for r in rows if r["chunk_type"] == "parent"]
    parent_by_rev = {r["revision_id"]: r for r in parent_rows}
    frozen_by_text = {r["vector_text"]: r["vector"] for r in rows if r.get("vector_text")}
    dim = table.schema.field("vector").type.list_size
    print(f"snapshot parents={len(parent_rows)} frozen_texts={len(frozen_by_text)} dim={dim}", flush=True)

    children: dict[str, dict[str, list[dict]]] = {"base": {}, "ai15": {}}
    missing_prefix = 0
    for rid, data in revisions.items():
        children["base"][rid] = build_child_documents(data)
        compact = str((prefixes.get(rid) or {}).get("compact") or "")
        if not compact:
            missing_prefix += 1
        children["ai15"][rid] = build_child_documents({**data, "ai_profile_compact": compact})
    kinds = {name: Counter(c["kind"] for cs in by_rev.values() for c in cs) for name, by_rev in children.items()}
    print(f"children={ {name: dict(c) for name, c in kinds.items()} } missing_prefix={missing_prefix}", flush=True)

    needed = {c["vector_text"] for by_rev in children.values() for cs in by_rev.values() for c in cs}
    missing = sorted(t for t in needed if t not in frozen_by_text)
    print(f"need_embedding={len(missing)}", flush=True)

    out_base = EVAL_DIR / "index"
    if out_base.exists():
        shutil.rmtree(out_base)
    degraded_log: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=180) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])

        new_vec: dict[str, list[float]] = {}
        batches = [missing[i:i + BATCH] for i in range(0, len(missing), BATCH)]
        embedding_semaphore = asyncio.Semaphore(6)

        async def embed_batch(batch: list[str]) -> None:
            async with embedding_semaphore:
                for text, vector in zip(batch, await emb.embed_documents(batch)):
                    new_vec[text] = vector

        for i in range(0, len(batches), 6):
            await asyncio.gather(*(embed_batch(batch) for batch in batches[i:i + 6]))
            if (i // 6) % 20 == 0:
                print(f"  embedded {min(i + 6, len(batches)) * BATCH}/{len(missing)}", flush=True)

        def resolve(text: str) -> list[float]:
            return frozen_by_text.get(text) or new_vec[text]

        def build_index(name: str):
            chunks = [to_chunk(r) for r in parent_rows]
            for rid, cs in children[name].items():
                parent = parent_by_rev.get(rid)
                if parent is None:
                    continue
                chunks.extend(child_chunk(parent, c, resolve(c["vector_text"])) for c in cs)
            index = LanceDBSearchIndex(out_base / name, vector_dimension=dim,
                                       embedding_model=settings["siliconflow_embedding_model"])
            index.upsert(chunks)
            print(f"{name}: wrote {len(chunks)} chunks", flush=True)
            return index

        indexes = {name: build_index(name) for name in ("base", "ai15")}
        filters = CandidateFilters()

        async def run_variant(name: str) -> dict:
            service = HybridSearchService(index=indexes[name], embedding_provider=emb,
                                          reranker_provider=reranker, search_timeout=30)
            result: dict[str, dict] = {}
            degraded: dict[str, dict] = {}
            for qid, intent in queries.items():
                entry: dict = {"intent": intent}
                for mode in MODES:
                    page = await service.search(intent, filters, limit=LIMIT, mode=mode)
                    entry[mode] = [alias(hit.candidate_id) for hit in page.items]
                    if page.degraded_reasons:
                        degraded.setdefault(qid, {})[mode] = list(page.degraded_reasons)
                result[qid] = entry
            degraded_log[name] = degraded
            print(f"{name}: done, degraded_queries={len(degraded)}", flush=True)
            return result

        retrieval = {name: await run_variant(name) for name in ("base", "ai15")}

    retrieval_dir = EVAL_DIR / "retrieval"
    retrieval_dir.mkdir(parents=True, exist_ok=True)
    for name, result in retrieval.items():
        (retrieval_dir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    (EVAL_DIR / "degraded").mkdir(parents=True, exist_ok=True)
    for name, degraded in degraded_log.items():
        (EVAL_DIR / "degraded" / f"{name}.json").write_text(json.dumps(degraded, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
    for name, result in retrieval.items():
        counts = {mode: Counter(len(entry[mode]) for entry in result.values()) for mode in MODES}
        print(f"{name}: sizes={ {m: dict(c) for m, c in counts.items()} }", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
