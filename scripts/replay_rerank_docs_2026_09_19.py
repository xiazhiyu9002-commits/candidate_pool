"""第 4.3 步：采集 (RRF 排名, rerank 分) 快照，用于零成本回放「重排文档数」。

为什么要单独采集：现有 ``.tmp-judge/search_results.json`` 只存了候选人别名，没有分数，
无法回答「把重排文档数从 100 缩到 50 会不会掉质量」。

做法（与生产 ``HybridSearchService.search`` 的混合路径一致）：
1. 直接调 ``service._parallel_retrieve`` 拿到 **RRF 融合后的顺序**（重排前的真实顺序）；
2. 对前 ``--rerank-docs`` 条调真实 reranker 拿 **真实相关性分**；
3. 落盘 ``[{alias, rrf_rank, rerank}]``（rerank 为 null 表示未被重排）。

有了这两个量，就能在本地复刻生产的两步（重排排序 → 按候选人去重截断），
把任意 ``K``（只重排 RRF 前 K 条）的结果重算出来 —— 零额外 API 调用。

安全：只读 ``.dev-data``；不调用 ``optimize_pending()``；候选人用 sha256 别名。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-plan"
SNAPSHOT = OUT / "rerank_snapshot.json"
POOL = 250          # 采集时的大池子：保证 K=250 档也有数据
RERANK_DOCS = 250   # 采集时重排这么多条，回放才能在 50~250 间任意取 K


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_settings() -> dict:
    return json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))


async def collect(qids: list[str], phrasing: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    queries = json.loads((ROOT / ".tmp-judge" / "queries.json").read_text(encoding="utf-8"))

    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    done = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {}
    pending = [q for q in qids if q not in done]
    print(f"[采集] 模式=hybrid phrasing={phrasing} 池子={POOL} 重排={RERANK_DOCS} "
          f"已完成={len(done)} 待跑={len(pending)}", flush=True)

    async with httpx.AsyncClient(timeout=120) as client:
        embedding = SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_reranker_model"])
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                     reranker_provider=reranker, search_timeout=120)

        for i, qid in enumerate(pending, 1):
            text = queries[qid][phrasing]
            degraded: list[str] = []
            budget = time.monotonic() + 120
            t0 = time.monotonic()
            fused, semantic_query, _ = await service._parallel_retrieve(
                text, CandidateFilters(), POOL, budget, degraded)
            contents = [h.vector_text or h.content for h in fused[:RERANK_DOCS]]
            scored = await reranker.rerank_scored(semantic_query, contents)
            score_map = {int(idx): float(s) for idx, s in scored if isinstance(idx, int)}

            items = [
                {"alias": alias(hit.candidate_id), "rrf_rank": rank,
                 "rerank": score_map.get(rank)}
                for rank, hit in enumerate(fused)
            ]
            done[qid] = {"phrasing": phrasing, "pool": POOL, "rerank_docs": RERANK_DOCS,
                         "query": semantic_query, "fused": len(fused),
                         "degraded": list(degraded), "items": items}
            SNAPSHOT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            print(f"  [{i:>2}/{len(pending)}] {qid} fused={len(fused)} "
                  f"重排={len(score_map)} 用时={time.monotonic() - t0:.1f}s "
                  f"降级={degraded}", flush=True)

    print(f"[采集] 完成，产物 {SNAPSHOT}")


def replay_with_k(snapshot: dict, k: int, top_n: int) -> dict[str, list[str]]:
    """只重排 RRF 前 K 条，其余保持 RRF 顺序追加（与生产 ``_apply_rerank_scored`` 一致）。"""
    result: dict[str, list[str]] = {}
    for qid, entry in snapshot.items():
        items = entry["items"]
        head = [it for it in items if it["rrf_rank"] < k and it["rerank"] is not None]
        head.sort(key=lambda it: (-it["rerank"], it["rrf_rank"]))
        tail = [it for it in items if it["rrf_rank"] >= k or it["rerank"] is None]
        seen: set[str] = set()
        picked: list[str] = []
        for it in list(head) + tail:
            if it["alias"] in seen:
                continue
            seen.add(it["alias"])
            picked.append(it["alias"])
            if len(picked) >= top_n:
                break
        result[qid] = picked
    return result


def evaluate(snapshot: dict, top_n: int, ks: list[int]) -> None:
    baseline: dict[str, list[str]] = {}
    max_k = max(ks)
    for qid, entry in snapshot.items():
        items = entry["items"]
        head = [it for it in items if it["rrf_rank"] < max_k and it["rerank"] is not None]
        head.sort(key=lambda it: (-it["rerank"], it["rrf_rank"]))
        tail = [it for it in items if it["rrf_rank"] >= max_k or it["rerank"] is None]
        seen: set[str] = set()
        picked: list[str] = []
        for it in list(head) + tail:
            if it["alias"] in seen:
                continue
            seen.add(it["alias"])
            picked.append(it["alias"])
            if len(picked) >= top_n:
                break
        baseline[qid] = picked

    print()
    print(f"=== 重排文档数回放（top_n={top_n}，基准 K={max_k}）===")
    print(f"{'K':>6}{'恰等于基准集合':>16}{'平均重合率':>12}{'平均新增':>10}{'平均丢失':>10}")
    for k in ks:
        got = replay_with_k(snapshot, k, top_n)
        exact = 0
        overlaps, adds, losses = [], [], []
        for qid, base in baseline.items():
            g = set(got[qid])
            b = set(base)
            if g == b:
                exact += 1
            overlaps.append(len(g & b) / max(1, len(b)))
            adds.append(len(g - b))
            losses.append(len(b - g))
        n = len(baseline)
        print(f"{k:>6}{f'{exact}/{n}':>16}{sum(overlaps) / n:>12.4f}"
              f"{sum(adds) / n:>10.2f}{sum(losses) / n:>10.2f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true", help="采集快照（消耗真实 API）")
    parser.add_argument("--phrasing", default="standard",
                        choices=("standard", "colloquial", "vague"))
    parser.add_argument("--qids", default="", help="逗号分隔；留空=全部 20 条")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--ks", default="50,80,100,150,200,250")
    args = parser.parse_args()

    if args.collect:
        all_qids = [f"J{i:02d}" for i in range(1, 21)]
        qids = [q.strip() for q in args.qids.split(",") if q.strip()] or all_qids
        asyncio.run(collect(qids, args.phrasing))
        return

    if not SNAPSHOT.exists():
        raise SystemExit(f"缺少快照 {SNAPSHOT}，先跑 --collect")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    print(f"快照查询数={len(snapshot)} / 池子={POOL} / 已重排条数={RERANK_DOCS}")
    evaluate(snapshot, args.top_n, ks)


if __name__ == "__main__":
    main()
