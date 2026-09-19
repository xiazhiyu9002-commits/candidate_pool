"""方案 2 检索评测：混合检索「仅 reranker 去前缀」。

索引沿用方案 1 的 ``base``（子向量仍带生产前缀），只把送进 reranker 的文档文本
裁掉已知画像前缀；对照组直接复用 ``base/hybrid``（同一索引 + 真实 reranker 直连）。

落盘 ``.tmp-prefix-eval/retrieval/rerank_prefixless.json``，并打印裁剪/未命中计数。
不碰 ``.dev-data``；密钥只用于请求头，不打印。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
INDEX_DIR = EVAL_DIR / "index" / "base"
LIMIT = 100
MODES = ("hybrid",)


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def collect_prefixes(data: dict) -> str:
    """与 ``documents._text`` 同样的换行压平，保证能匹配到写入索引的文本。"""
    from kerui_recruit.search.documents import _compact_profile

    return _compact_profile(data).replace("\n", " ").replace("\r", " ")


class PrefixStrippingReranker:
    """只裁剪已知画像前缀，其余行为完全透传给真实 reranker。"""

    def __init__(self, inner, prefixes) -> None:
        # 按首 8 字符分桶，桶内最长优先，减少逐条比对成本。
        self._inner = inner
        buckets: dict[str, list[str]] = {}
        for prefix in sorted(set(p for p in prefixes if p), key=len, reverse=True):
            buckets.setdefault(prefix[:8], []).append(prefix + " ")
        self._buckets = buckets
        self.stripped = 0
        self.unmatched = 0

    def strip(self, document: str) -> str:
        for prefix in self._buckets.get(document[:8], ()):
            if document.startswith(prefix):
                self.stripped += 1
                return document[len(prefix):]
        self.unmatched += 1
        return document

    async def rerank_scored(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return await self._inner.rerank_scored(query, [self.strip(doc) for doc in documents])

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        return await self._inner.rerank(query, [self.strip(doc) for doc in documents])


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider, SiliconFlowRerankerProvider
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.service import HybridSearchService

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    conn = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    rows = conn.execute(
        "SELECT r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ).fetchall()
    conn.close()
    prefixes = {collect_prefixes(json.loads(raw) if isinstance(raw, str) else raw or {}) for raw, in rows}
    prefixes.discard("")
    print(f"known_prefixes={len(prefixes)}", flush=True)

    intents = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    queries = {qid: item["intent"] for qid, item in intents.items() if qid <= "Q24"}

    async with httpx.AsyncClient(timeout=180) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        inner = SiliconFlowRerankerProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])
        wrapper = PrefixStrippingReranker(inner, prefixes)
        index = LanceDBSearchIndex(INDEX_DIR, vector_dimension=emb.dimension,
                                   embedding_model=settings["siliconflow_embedding_model"])
        service = HybridSearchService(index=index, embedding_provider=emb,
                                      reranker_provider=wrapper, search_timeout=30)
        filters = CandidateFilters()
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
            print(qid, len(entry["hybrid"]), flush=True)

    retrieval_dir = EVAL_DIR / "retrieval"
    retrieval_dir.mkdir(parents=True, exist_ok=True)
    (retrieval_dir / "rerank_prefixless.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
    (EVAL_DIR / "degraded").mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "degraded" / "rerank_prefixless.json").write_text(
        json.dumps({"queries": degraded, "stripped": wrapper.stripped, "unmatched": wrapper.unmatched},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"stripped={wrapper.stripped} unmatched={wrapper.unmatched} degraded_queries={len(degraded)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
