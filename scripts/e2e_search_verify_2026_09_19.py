"""§8 端到端验收：真实库 + 真实 embedding/reranker，覆盖向量与混合检索。

验证点（都是本次改动直接影响的）：
1. `VECTOR_MIN_SIMILARITY = None`（哨兵）后，向量模式不再被绝对下限清空；
2. `limit` ∈ {20, 50, 100} 时返回条数与 `limit` 一致，且头部顺序稳定（前 20 一致）；
3. 三阶段质量链生效：拿到重排分时 `RERANK_MIN_SCORE` 把关，重排失败时回落绝对值兜底；
4. 无意外降级（`degraded_reasons` 为空），记录 P50/P95 耗时。

安全：只读 `.dev-data`；不调用 `optimize_pending()`；候选人 ID 用 sha256 别名。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

import kerui_recruit.search.service as service_module  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-plan" / "e2e_search.json"
MODES = ("vector", "hybrid")
LIMITS = (20, 50, 100)
QUERIES = 8


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class CachingEmbedding:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.cache: dict[str, list[float]] = {}
        self.calls = 0

    async def embed_documents(self, texts):
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str):
        if text not in self.cache:
            self.cache[text] = await self.inner.embed_query(text)
            self.calls += 1
        return self.cache[text]


async def main() -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    queries = json.loads((ROOT / ".tmp-real-eval" / "real_queries.json").read_text(encoding="utf-8"))
    cases = [(q["qid"], q["text"]) for q in queries][:QUERIES]

    print(f"有效常量: VECTOR_MIN_SIMILARITY={service_module.VECTOR_MIN_SIMILARITY!r} "
          f"FUSION={service_module.VECTOR_FUSION_MIN_SIMILARITY!r} "
          f"FALLBACK={service_module.VECTOR_MIN_SIMILARITY_FALLBACK!r} "
          f"RERANK_MIN_SCORE={service_module.RERANK_MIN_SCORE!r} "
          f"RERANK_DOCS={service_module.RERANK_DOCS} RECALL_MIN={service_module.RECALL_MIN}")

    async with httpx.AsyncClient(timeout=120) as client:
        embedding = CachingEmbedding(SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"]))
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_reranker_model"])
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=reranker, search_timeout=120)

        report: dict = {}
        for mode in MODES:
            for limit in LIMITS:
                heads, counts, elapsed, degraded, empties, scored_ok = [], [], [], 0, 0, 0
                for qid, text in cases:
                    started = time.monotonic()
                    page = await service.search(text, CandidateFilters(), limit=limit, mode=mode)
                    elapsed.append((time.monotonic() - started) * 1000)
                    ids = []
                    for hit in page.items:
                        key_id = alias(hit.candidate_id)
                        if key_id not in ids:
                            ids.append(key_id)
                    heads.append(ids[:20])
                    counts.append(len(ids))
                    if page.degraded_reasons:
                        degraded += 1
                    if not ids:
                        empties += 1
                    if any(hit.rerank_score is not None for hit in page.items):
                        scored_ok += 1
                key_name = f"{mode}@limit{limit}"
                report[key_name] = {
                    "返回条数": counts,
                    "前20": heads,
                    "空结果": empties,
                    "降级次数": degraded,
                    "重排可用次数": scored_ok,
                    "P50ms": round(statistics.median(elapsed), 1),
                    "P95ms": round(sorted(elapsed)[max(0, int(len(elapsed) * 0.95) - 1)], 1),
                }
                print(f"  {key_name:<18} 返回{counts} 空={empties} 降级={degraded} "
                      f"重排可用={scored_ok} P50={report[key_name]['P50ms']}ms")

        # limit 只应改变返回条数，不应改变头部顺序：同模式下 limit=20 与 limit=100 的前 20 必须一致。
        print("\n--- limit 不应改变前 20 顺序（limit20 vs limit100 逐查询比对）---")
        for mode in MODES:
            identical = 0
            for a, b in zip(report[f"{mode}@limit20"]["前20"], report[f"{mode}@limit100"]["前20"]):
                if a == b[:len(a)]:
                    identical += 1
            print(f"  {mode}: 前 20 完全一致 {identical}/{len(cases)} 条查询")

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n查询向量 API 调用={embedding.calls}  产物={OUT}")


if __name__ == "__main__":
    asyncio.run(main())
