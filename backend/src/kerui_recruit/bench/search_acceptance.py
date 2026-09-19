"""Offline HTTP/search acceptance; build and measure in separate Python processes.

Uses synthetic SQLite facts and a real LanceDB projection. No provider SDK,
credentials, external network, or real resumes are loaded. Local hash vectors
exercise vector retrieval cost, not real embedding quality or relevance.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy import func, insert, select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.search import router
from kerui_recruit.db.base import Base
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.local import LocalHashEmbeddingProvider, LocalKeywordReranker
from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


DIMENSION = 64
MODEL = "benchmark-local-hash-64-v1"


def open_index(root):
    return LanceDBSearchIndex(root / "index", vector_dimension=DIMENSION, embedding_model=MODEL)


def build(root: Path, count: int):
    root.mkdir(parents=True, exist_ok=True)
    if (root / "facts.sqlite").exists():
        raise ValueError("Use an empty temporary directory; benchmark never overwrites data")
    started = time.perf_counter()
    engine = create_engine_for(root / "facts.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    index = open_index(root)
    embedding = LocalHashEmbeddingProvider(dimension=DIMENSION)
    vectors = {}
    for offset in range(0, count, 5000):
        candidates, documents, revisions, blobs, chunks = [], [], [], [], []
        for number in range(offset, min(offset + 5000, count)):
            cid, rid = f"candidate-{number:06}", f"revision-{number:06}"
            did, bid = f"document-{number:06}", f"blob-{number:06}"
            skill = ("Python", "Java", "Go", "Rust")[number % 4]
            years = float(number % 15 + 1)
            degree = ("MASTER", "BACHELOR")[number % 2]
            location = ("上海", "北京", "深圳")[number % 3]
            preferred = ["杭州", "广州"] if number % 2 else ["北京", "上海"]
            parts = [f"{skill} backend 金融 payments family {number % 100}",
                     f"{skill} project 数据 warehouse risk systems", f"{skill} education operations experience"]
            if number % 10 == 0:
                parts[2] += " Java"
            data = dict(total_years=years, highest_degree=degree, location=location,
                        preferred_locations=preferred, skills=[skill])
            digest = f"{number:064x}"
            candidates.append(dict(id=cid, display_name=f"Synthetic {number}", status="AVAILABLE",
                                   total_years=years, highest_degree=degree))
            blobs.append(dict(id=bid, content_sha256=digest, suffix=".txt", size_bytes=128,
                              storage_path=f"synthetic/{bid}", reference_count=1))
            documents.append(dict(id=did, candidate_id=cid))
            revisions.append(dict(id=rid, document_id=did, blob_id=bid, content_sha256=digest,
                                  original_filename=f"synthetic-{number}.txt", status="READY", is_current=True,
                                  raw_text="\n".join(parts), parsed_data=data))
            for part, content in enumerate(parts):
                vector = vectors.setdefault(content, tuple(embedding._embed(content)))
                chunks.append(SearchChunk(f"{rid}:{part}", cid, rid, content, vector, years,
                                           degree, location, "AVAILABLE", preferred_locations=tuple(preferred)))
        with factory.begin() as session:
            for model, rows in ((Candidate, candidates), (Blob, blobs), (ResumeDocument, documents), (ResumeRevision, revisions)):
                session.execute(insert(model), rows)
        index.upsert(chunks)
        print(json.dumps({"built_candidates": min(offset + 5000, count), "seconds": round(time.perf_counter() - started, 2)}), flush=True)
    index.optimize_pending()
    with factory() as session:
        sqlite_count = session.scalar(select(func.count()).select_from(Candidate))
    table = index.database.open_table(index.table_name)
    unique_candidates = len(table.search(None).select(["candidate_id"]).limit(None).to_arrow()["candidate_id"].unique())
    manifest = dict(candidate_count=sqlite_count, indexed_candidate_count=unique_candidates,
                    chunk_count=table.count_rows(), chunks_per_candidate=3, embedding_dimension=DIMENSION,
                    embedding_model=MODEL, build_seconds=round(time.perf_counter() - started, 2),
                    synthetic=True, external_provider_calls=0)
    assert sqlite_count == unique_candidates == count
    assert manifest["chunk_count"] == count * 3
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest), flush=True)


def stats(samples):
    values = sorted(sample["elapsed_ms"] for sample in samples)
    def percentile(fraction):
        return values[min(len(values) - 1, round((len(values) - 1) * fraction))] if values else None
    return dict(requests=len(samples), p50_ms=percentile(.5), p95_ms=percentile(.95),
                max_ms=max(values, default=None), http_errors=sum(sample["status"] != 200 for sample in samples),
                empty_responses=sum(sample["items"] == 0 for sample in samples),
                degraded=dict(Counter(reason for sample in samples for reason in sample["degraded_reasons"])))


async def measure(root: Path, output: Path, query_count: int, concurrency: int):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    engine = create_engine_for(root / "facts.sqlite")
    factory = sessionmaker(engine, expire_on_commit=False)
    index = open_index(root)
    embedding = LocalHashEmbeddingProvider(dimension=DIMENSION)
    reranker = LocalKeywordReranker()
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(session_factory=factory, encryption_service=None,
        search_service=HybridSearchService(index=index, embedding_provider=embedding, reranker_provider=reranker))
    queries = ["Python 金融", "Java backend", "Go warehouse", "本科 上海", "意向广州", "Python 排除Java"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchmark") as client:
        async def request(query):
            started = time.perf_counter()
            response = await client.post("/api/search/candidates", json={"query": query, "limit": 20})
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            body = response.json()
            return dict(query=query, elapsed_ms=elapsed, status=response.status_code,
                        items=len(body.get("items", [])), degraded_reasons=body.get("degraded_reasons", []),
                        empty_reason=body.get("empty_reason"))
        cold = await request(queries[0])
        hot = [await request(queries[n % len(queries)]) for n in range(12)]
        semaphore = asyncio.Semaphore(concurrency)
        async def concurrent(n):
            async with semaphore:
                return await request(queries[n % len(queries)])
        concurrent_samples = await asyncio.gather(*(concurrent(n) for n in range(query_count)))
        deadline_samples = []
        class SlowEmbedding:
            async def embed_query(self, text):
                await asyncio.sleep(20)
                return [0.] * DIMENSION
        class SlowReranker:
            async def rerank(self, query, documents):
                await asyncio.sleep(20)
                return list(range(len(documents)))
        for dependency, budget in (("embedding", .2), ("reranker", .2), ("embedding", 1.), ("reranker", 1.)):
            app.state.services.search_service = HybridSearchService(
                index=index, embedding_provider=SlowEmbedding() if dependency == "embedding" else embedding,
                reranker_provider=SlowReranker() if dependency == "reranker" else reranker, search_timeout=budget)
            sample = await request("Python 金融")
            deadline_samples.append(dict(dependency=dependency, hard_budget_ms=round(budget * 1000), **sample))
    report = dict(manifest, first_request=cold, hot_sequential=stats(hot),
                  concurrent=stats(concurrent_samples), concurrency=concurrency,
                  deadline_degradation=deadline_samples,
                  limitations=["First request is a fresh Python process/index connection; OS disk cache was not flushed.",
                               "HTTP uses ASGI transport and real route/SQLite hydration, not TCP or desktop UI.",
                               "64-dimensional local hash embeddings and keyword reranker measure cost, not semantic quality.",
                               "Short repeated synthetic evidence; real long CVs, high-dimensional models and network provider latency are not represented.",
                               "Concurrent machine activity and OS scheduling can affect timings; no controlled hardware isolation."],
                  samples=dict(hot=hot, concurrent=concurrent_samples))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("build", "measure"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidates", type=int, default=100000)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if options.phase == "build":
        build(options.root, options.candidates)
    else:
        asyncio.run(measure(options.root, options.output or options.root / "report.json", options.queries, options.concurrency))


if __name__ == "__main__":
    main()
