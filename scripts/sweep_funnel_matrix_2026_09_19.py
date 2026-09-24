"""§4.5 扫描矩阵：四旋钮（pool_limit / RECALL_MIN / RERANK_DOCS / limit）在真实数据上的最优档位。

设计要点：
- **确定性 reranker**：用 ``LocalKeywordReranker``。远程 reranker 抖动会让同一参数两次运行
  top20 交集低至 0.84，参数差异会被噪声淹没（见方案 §0.5.4）。
- **查询向量缓存**：99 条查询只 embed 一次，之后各档位复用，把 API 调用从「配置数 × 查询数」
  降到「查询数」。
- **常量注入**：通过 monkeypatch 模块级常量与 ``_pool_limit``，不改源码。
- 标签来自 ``.tmp-real-eval/real_labels.json``（键为 sha256 别名，与检索侧口径一致）。

安全：只读 ``.dev-data``；不调用 ``optimize_pending()``；候选人 ID 用 sha256 别名。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

import kerui_recruit.search.service as service_module  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.evaluation.retrieval import ndcg_at_k, recall_at_k  # noqa: E402
from kerui_recruit.providers.local import LocalKeywordReranker  # noqa: E402
from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider  # noqa: E402
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL = ROOT / ".tmp-real-eval"
OUT = ROOT / ".tmp-plan" / "sweep_matrix.json"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class CachingEmbedding:
    """按文本缓存查询向量：99 条查询只打一次 API。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.cache: dict[str, list[float]] = {}
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        if text not in self.cache:
            self.cache[text] = await self.inner.embed_query(text)
            self.calls += 1
        return self.cache[text]


def load_cases() -> list[tuple[str, str, dict]]:
    queries = json.loads((EVAL / "real_queries.json").read_text(encoding="utf-8"))
    labels = json.loads((EVAL / "real_labels.json").read_text(encoding="utf-8"))
    cases = []
    for query in queries:
        relevant = {cid: int(g) for cid, g in (labels.get(query["qid"]) or {}).items()}
        if relevant:
            cases.append((query["qid"], query["text"], relevant))
    return cases


# (标签, pool_limit, RECALL_MIN, RERANK_DOCS, limit)
# 只保留**决策相关**的档位：单配置实测约 7 分钟，全矩阵 14 档需 1.5 小时以上，
# 故按「能否改变结论」裁剪 —— 边际档位（pool=100、rerank=400、recall=100 等）对决策无增量。
CONFIGS: list[tuple[str, int, int, int, int]] = [
    ("现役 pool120/recall100/rerank100/limit100", 120, 100, 100, 100),
    # limit 扫描：池子与重排都放到 200，使重排 > limit，重排才有「选人」能力
    ("limit=20  pool200/recall200/rerank200", 200, 200, 200, 20),
    ("limit=50  pool200/recall200/rerank200", 200, 200, 200, 50),
    ("limit=100 pool200/recall200/rerank200", 200, 200, 200, 100),
    # pool 扫描：固定 limit=50 / rerank=200（rerank=200 在 pool=200 档即上一条）
    ("pool=120  recall200/rerank200/limit50", 120, 200, 200, 50),
    ("pool=400  recall200/rerank200/limit50", 400, 200, 200, 50),
    # 重排窗口扫描：固定 pool=400 / limit=50（rerank=200 在 pool=400 档即上一条）
    ("rerank=50  pool400/recall200/limit50", 400, 200, 50, 50),
]


async def run_config(service: HybridSearchService, cases, pool: int, recall: int,
                     rerank: int, limit: int) -> dict:
    service_module._pool_limit = lambda value, _p=pool: _p
    service_module.RECALL_MIN = recall
    service_module.RERANK_DOCS = rerank
    # 指标函数在「无正相关标签」时返回 None，按模块约定不并入平均（见 evaluation/retrieval.py）。
    collected: dict[str, list[float]] = {"R@20": [], "R@50": [], "NDCG@10": [], "P@5": []}
    empty = 0
    returned: list[int] = []
    skipped = 0
    for _qid, text, relevant in cases:
        page = await service.search(text, CandidateFilters(), limit=limit, mode="hybrid")
        ranked, seen = [], set()
        for hit in page.items:
            key = alias(hit.candidate_id)
            if key not in seen:
                seen.add(key)
                ranked.append(key)
        r20 = recall_at_k(ranked, relevant, 20)
        if r20 is None:
            skipped += 1
            continue
        collected["R@20"].append(r20)
        collected["R@50"].append(recall_at_k(ranked, relevant, 50))
        collected["NDCG@10"].append(ndcg_at_k(ranked, relevant, 10))
        collected["P@5"].append(
            sum(1 for c in ranked[:5] if relevant.get(c, 0) >= 2) / 5 if ranked else 0.0)
        empty += 0 if ranked else 1
        returned.append(len(ranked))

    def mean(key: str) -> float:
        values = [v for v in collected[key] if v is not None]
        return sum(values) / len(values) if values else 0.0

    return {
        "R@20": mean("R@20"), "R@50": mean("R@50"),
        "NDCG@10": mean("NDCG@10"), "P@5": mean("P@5"),
        "empty": empty, "skipped": skipped,
        "returned_median": sorted(returned)[len(returned) // 2] if returned else 0,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="只跑标签包含该子串的配置")
    args = parser.parse_args()

    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")
    cases = load_cases()
    print(f"带标签查询={len(cases)} 配置数={len(CONFIGS)}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}

    async with httpx.AsyncClient(timeout=90) as client:
        embedding = CachingEmbedding(SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"]))
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=LocalKeywordReranker(), search_timeout=90)

        rows = []
        for label, pool, recall, rerank, limit in CONFIGS:
            if args.only and args.only not in label:
                continue
            if label in done:
                agg = done[label]
            else:
                agg = await run_config(service, cases, pool, recall, rerank, limit)
                done[label] = agg
                OUT.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
            rows.append((label, agg))
            print(f"  {label:<46} R@20={agg['R@20']:.4f} NDCG@10={agg['NDCG@10']:.4f} "
                  f"P@5={agg['P@5']:.4f} 空={agg['empty']} 返回中位={agg['returned_median']}",
                  flush=True)

    print()
    print(f"{'配置':<46}{'R@20':>8}{'R@50':>8}{'NDCG@10':>9}{'P@5':>8}{'空':>4}{'返回':>5}")
    for label, agg in rows:
        print(f"{label:<46}{agg['R@20']:>8.4f}{agg['R@50']:>8.4f}{agg['NDCG@10']:>9.4f}"
              f"{agg['P@5']:>8.4f}{agg['empty']:>4}{agg['returned_median']:>5}")
    print(f"\n查询向量 API 调用={embedding.calls}（缓存生效）  产物={OUT}")


if __name__ == "__main__":
    asyncio.run(main())
