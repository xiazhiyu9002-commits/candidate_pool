"""画像分块 v3 混合检索消融：旧索引 vs 双形态+前缀。

同快照 + 同 embedding/rerank，走完整 HybridSearchService（FTS+向量融合）复算混合 P@5。
「旧索引」用冻结快照的父/子行重建为当前 schema（复用原向量），「双形态+前缀」用新子切片。
写隔离索引，不碰 .dev-data。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path

import httpx
import lancedb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
SNAP = ROOT / ".semantic-audit-snapshot"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def build_children(data: dict) -> list[dict]:
    from kerui_recruit.search.documents import build_child_documents
    return build_child_documents(data)


def _to_chunk(r: dict, *, kind: str, sequence: int | None, evidence: list[str]) -> "object":
    from kerui_recruit.search.contracts import SearchChunk
    return SearchChunk(
        id=r["id"], candidate_id=r["candidate_id"], revision_id=r["revision_id"],
        content=r.get("keyword_text") or r.get("content") or "",
        vector=tuple(float(x) for x in r["vector"]),
        total_years=r.get("total_years"), highest_degree=r.get("highest_degree"),
        location=r.get("location"), candidate_status=r.get("candidate_status", "AVAILABLE"),
        qs_rank=r.get("qs_rank"), school_level=r.get("school_level"),
        preferred_location=r.get("preferred_location"),
        preferred_locations=tuple(r.get("preferred_locations") or ()),
        keyword_text=r.get("keyword_text"), keyword_index_text=r.get("keyword_index_text"),
        vector_text=r.get("vector_text"), body_index_text=r.get("body_index_text") or "",
        chunk_type=r.get("chunk_type", "parent"), parent_id=r.get("parent_id"),
        kind=kind, sequence=sequence, evidence_path=tuple(evidence),
        name_terms=tuple(r.get("name_terms") or ()), school_terms=tuple(r.get("school_terms") or ()),
        company_terms=tuple(r.get("company_terms") or ()), title_terms=tuple(r.get("title_terms") or ()),
        location_terms=tuple(r.get("location_terms") or ()), skills=tuple(r.get("skills") or ()),
        age=r.get("age"), school_tags=tuple(r.get("school_tags") or ()),
        direction=r.get("direction"), school_region=r.get("school_region"),
        specializations=tuple(r.get("specializations") or ()),
    )


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
    from kerui_recruit.providers.fakes import FakeRerankerProvider
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.service import HybridSearchService
    from kerui_recruit.search.contracts import CandidateFilters, SearchChunk

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])
    retrieval = json.loads((SNAP / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    labels = json.loads((SNAP / "blind_labels.json").read_text(encoding="utf-8"))

    import sqlite3
    conn = sqlite3.connect(SNAP / "recruit.sqlite3")
    parsed_by_rev = {}
    for rid, p in conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ):
        parsed_by_rev[rid] = json.loads(p) if p else {}
    conn.close()

    # 读冻结快照（旧索引）
    db = lancedb.connect(str(SNAP / "search"))
    table = db.open_table("candidate_chunks")
    rows = table.search(None).limit(None).to_list()
    parent_rows = [r for r in rows if r["chunk_type"] == "parent"]
    old_child_rows = [r for r in rows if r["chunk_type"] == "child"]
    frozen_by_text = {r["vector_text"]: r["vector"] for r in rows if r.get("vector_text")}
    dim = table.schema.field("vector").type.list_size
    print(f"old: parent={len(parent_rows)} child={len(old_child_rows)} dim={dim}", flush=True)

    # 双形态+前缀子文档（每修订）
    dual_children: dict[str, list[dict]] = {}
    for rid, data in parsed_by_rev.items():
        dual_children[rid] = build_children(data)

    # 需要新 embedding 的文本（冻结已有则复用）
    missing_texts: set[str] = set()
    for children in dual_children.values():
        for c in children:
            if c["vector_text"] not in frozen_by_text:
                missing_texts.add(c["vector_text"])
    missing = list(missing_texts)
    print(f"需新 embedding 文本数：{len(missing)}", flush=True)

    async with httpx.AsyncClient(timeout=180) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        new_vec = {}
        for i in range(0, len(missing), 25):
            batch = missing[i:i + 25]
            vecs = await emb.embed_documents(batch)
            for t, v in zip(batch, vecs):
                new_vec[t] = v
            if (i // 25) % 40 == 0:
                print(f"  embedded {i + len(batch)}/{len(missing)}", flush=True)

        def resolve(text):
            return frozen_by_text.get(text) or new_vec[text]

        # 旧索引：父 + 冻结子行（复用原向量）
        old_chunks = [_to_chunk(r, kind="parent", sequence=None, evidence=[]) for r in parent_rows]
        for r in old_child_rows:
            old_chunks.append(_to_chunk(r, kind="child", sequence=None, evidence=[]))

        # 双形态+前缀索引：父 + 新子切片
        dual_chunks = [_to_chunk(r, kind="parent", sequence=None, evidence=[]) for r in parent_rows]
        for rid, children in dual_children.items():
            parent = next((p for p in parent_rows if p["revision_id"] == rid), None)
            if parent is None:
                continue
            for c in children:
                dual_chunks.append(SearchChunk(
                    id=f"{rid}:{c['kind']}:{c.get('sequence')}",
                    candidate_id=parent["candidate_id"], revision_id=rid,
                    content=c["vector_text"], vector=tuple(resolve(c["vector_text"])),
                    total_years=parent.get("total_years"), highest_degree=parent.get("highest_degree"),
                    location=parent.get("location"), candidate_status=parent.get("candidate_status", "AVAILABLE"),
                    qs_rank=parent.get("qs_rank"), school_level=parent.get("school_level"),
                    preferred_location=parent.get("preferred_location"),
                    preferred_locations=tuple(parent.get("preferred_locations") or ()),
                    keyword_text=c["vector_text"], keyword_index_text=c.get("keyword_index_text") or "",
                    vector_text=c["vector_text"], body_index_text="",
                    chunk_type="child", parent_id=rid,
                    kind=c["kind"], sequence=c.get("sequence"), evidence_path=tuple(c.get("evidence_path") or ()),
                    name_terms=tuple(parent.get("name_terms") or ()), school_terms=tuple(parent.get("school_terms") or ()),
                    company_terms=tuple(parent.get("company_terms") or ()), title_terms=tuple(parent.get("title_terms") or ()),
                    location_terms=tuple(parent.get("location_terms") or ()), skills=tuple(parent.get("skills") or ()),
                    age=parent.get("age"), school_tags=tuple(parent.get("school_tags") or ()),
                    direction=parent.get("direction"), school_region=parent.get("school_region"),
                    specializations=tuple(parent.get("specializations") or ()),
                ))

        out_base = ROOT / ".tmp-profile-chunks-hybrid"
        if out_base.exists():
            shutil.rmtree(out_base)

        def build_index(name, chunks):
            idx = LanceDBSearchIndex(out_base / name, vector_dimension=dim,
                                     embedding_model=settings["siliconflow_embedding_model"])
            idx.upsert(chunks)
            print(f"{name}: wrote {len(chunks)} chunks", flush=True)
            return idx

        old_index = build_index("old", old_chunks)
        dual_index = build_index("dual_prefix", dual_chunks)

        def make_service(index):
            return HybridSearchService(
                index=index, embedding_provider=emb,
                reranker_provider=FakeRerankerProvider(), search_timeout=30)

        def p_at_k(ids, qid, k=5):
            judged = labels.get(qid, {})
            grades = [int(judged.get(cid, {}).get("grade", 0)) for cid in ids[:k]]
            return sum(1 for g in grades if g >= 2) / k

        for name, svc in (("old", make_service(old_index)), ("dual_prefix", make_service(dual_index))):
            p5 = []
            for qid, item in retrieval.items():
                if qid > "Q24":
                    continue
                page = await svc.search(item["intent"], CandidateFilters(), limit=20, mode="hybrid")
                ids = [alias(h.candidate_id) for h in page.items]
                p5.append(p_at_k(ids, qid))
            print(f"hybrid {name} P@5 = {round(sum(p5) / len(p5), 4)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
