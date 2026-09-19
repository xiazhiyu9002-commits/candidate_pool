"""关键词检索 + 精确筛选的完整 API 性能基线（P95，AI 调用为 0）。

走 FastAPI 路由（ASGITransport）测完整接口耗时（含 SQLite hydrate），
覆盖合同 5.2「关键词检索、精确筛选完整 API P95 ≤ 500ms」的测量口径。
使用本地 provider 不影响这两个方式（关键词/精确筛选在线 AI 调用为 0）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.search import router
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.local import LocalHashEmbeddingProvider, LocalKeywordReranker
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


def _p95(values: list[float]) -> float:
    values = sorted(values)
    idx = math.ceil(0.95 * len(values)) - 1
    return values[max(0, idx)]


async def measure(data_root: Path, n: int) -> dict:
    engine = create_engine_for(data_root / "db" / "recruit.sqlite3")
    factory = sessionmaker(engine, expire_on_commit=False)
    index = LanceDBSearchIndex(
        data_root / "search", vector_dimension=1024, embedding_model="BAAI/bge-m3"
    )
    svc = HybridSearchService(
        index=index,
        embedding_provider=LocalHashEmbeddingProvider(dimension=1024),
        reranker_provider=LocalKeywordReranker(),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(
        session_factory=factory, encryption_service=None, search_service=svc
    )

    keyword_queries = ["Java", "Python", "上海", "腾讯", "架构师", "LangChain", "风控", "支付", "大数据", "RAG"]
    filter_cases = [
        {"min_years": 10},
        {"highest_degree": "MASTER"},
        {"location": "上海"},
        {"school_level": "985"},
        {"max_qs_rank": 100},
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bench"
    ) as client:
        # 预热（线程池冷启动 + 索引打开），不计入常态指标。
        await client.post("/api/search/candidates",
                          json={"query": "Java", "mode": "keyword", "limit": 20})

        keyword_samples: list[float] = []
        for i in range(n):
            q = keyword_queries[i % len(keyword_queries)]
            t0 = time.perf_counter()
            resp = await client.post("/api/search/candidates",
                                     json={"query": q, "mode": "keyword", "limit": 20})
            keyword_samples.append((time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200, resp.text

        filter_samples: list[float] = []
        for i in range(n):
            f = filter_cases[i % len(filter_cases)]
            t0 = time.perf_counter()
            resp = await client.post("/api/search/candidates",
                                     json={"query": "", "mode": "keyword", "limit": 20,
                                           "filters": f})
            filter_samples.append((time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200, resp.text

    return {
        "keyword": {
            "requests": len(keyword_samples),
            "p50_ms": round(sorted(keyword_samples)[len(keyword_samples) // 2], 2),
            "p95_ms": round(_p95(keyword_samples), 2),
            "max_ms": round(max(keyword_samples), 2),
        },
        "exact_filter": {
            "requests": len(filter_samples),
            "p50_ms": round(sorted(filter_samples)[len(filter_samples) // 2], 2),
            "p95_ms": round(_p95(filter_samples), 2),
            "max_ms": round(max(filter_samples), 2),
        },
        "threshold_ms": 500,
        "keyword_p95_pass": _p95(keyword_samples) <= 500,
        "filter_p95_pass": _p95(filter_samples) <= 500,
        "note": "完整 API 耗时（含 SQLite hydrate）；本地 provider，关键词/精确筛选 AI 调用为 0",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path,
                        default=Path(r"c:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data"))
    parser.add_argument("--output", type=Path,
                        default=Path(r"docs\verification\selection-acceptance\perf.json"))
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()
    result = await measure(args.data_root, args.n)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
