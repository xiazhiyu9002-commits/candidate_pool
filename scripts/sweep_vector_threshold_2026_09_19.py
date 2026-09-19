"""分模式（vector / hybrid）扫描向量阈值参数，真实数据 + 真实 embedding/reranker。

- **vector**：不需要调 API。直接读索引的 ``_distance``（= L2 距离的平方）拿到无阈值的
  完整排序，再在本地按「绝对下限 × 相对比例」复刻生产过滤规则重算指标 —— 因此可以
  便宜地跑二维网格。
- **hybrid**：阈值在 RRF 融合之前作用于向量通道，候选集一变融合与 rerank 输入就变，
  无法用缓存近似，必须真的重跑。所以只扫一维（绝对下限）。

产物：``.tmp-real-eval/retrieval/real_hybrid@<abs>_<ratio>.json``（与既有评测同样的
``{qid: {"intent":..., "hybrid": [alias,...]}}`` 结构，便于复用指标脚本）。

安全：只读直连 ``.dev-data``；不调用 ``optimize_pending()``；ID 用 sha256 别名。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

import kerui_recruit.search.service as service_module  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.evaluation.retrieval import ndcg_at_k, recall_at_k  # noqa: E402
from kerui_recruit.providers.local import LocalKeywordReranker  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.query import parse_query  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL = ROOT / ".tmp-real-eval"
OUT = EVAL / "retrieval"
RAW = EVAL / "vector_threshold_sweep_raw.json"
VECTOR_GRID = ((0.0, 0.0), (0.55, 0.0), (0.575, 0.0), (0.60, 0.0), (0.62, 0.0),
               (0.65, 0.0), (0.6667, 0.0),
               (0.0, 0.9), (0.55, 0.9), (0.575, 0.9), (0.60, 0.9), (0.62, 0.9),
               (0.6667, 0.9))
METRICS = (("R@20", lambda r, rel: recall_at_k(r, rel, 20)),
           ("NDCG@10", lambda r, rel: ndcg_at_k(r, rel, 10)),
           ("P@5(g>=2)", None))


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_cases() -> list[tuple[str, dict]]:
    queries = json.loads((EVAL / "real_queries.json").read_text(encoding="utf-8"))
    labels = json.loads((EVAL / "real_labels.json").read_text(encoding="utf-8"))
    cases = []
    for query in queries:
        relevant = {cid: int(g) for cid, g in (labels.get(query["qid"]) or {}).items()}
        if relevant:
            cases.append((query["qid"], relevant))
    return cases


def mean(values):
    present = [v for v in values if isinstance(v, (int, float))]
    return sum(present) / len(present) if present else float("nan")


def apply_cut(ranked: list[tuple[str, float]], abs_cos: float,
              ratio: float) -> list[str]:
    """复刻生产规则：threshold = max(绝对下限, 相对下限)，在 score 空间等值换算到 cos。

    score = 1/(1+L2²) = 1/(3-2cos)；给定 top1 的 score，相对下限对应的 cos 由
    ``cos = (3 - 1/score) / 2`` 反解。
    """
    if not ranked:
        return []
    threshold = abs_cos
    if ratio > 0:
        relative_score = ratio / (3 - 2 * ranked[0][1])
        if relative_score > 0:
            threshold = max(threshold, (3 - 1.0 / relative_score) / 2)
    return [cid for cid, cos in ranked if cos >= threshold]


def score_of(ranked: list[str], relevant: dict) -> dict:
    ndcg = ndcg_at_k(ranked, relevant, 10)
    precision = (sum(1 for cid in ranked[:5] if relevant.get(cid, 0) >= 2) / 5
                 if ranked else None)
    return {"R@20": recall_at_k(ranked, relevant, 20),
            "NDCG@10": ndcg, "P@5(g>=2)": precision,
            "empty": not ranked}


def report(title: str, rows: list[tuple[str, dict]]) -> str:
    print()
    print(f"=== {title} ===")
    print(f"{'参数':<24}{'R@20':>9}{'NDCG@10':>10}{'P@5(g>=2)':>11}{'空结果':>7}")
    best_label = ""
    best_ndcg = float("-inf")
    for label, aggregate in rows:
        print(f"{label:<24}{aggregate['R@20']:>9.4f}{aggregate['NDCG@10']:>10.4f}"
              f"{aggregate['P@5(g>=2)']:>11.4f}{aggregate['empty']:>7}")
        if aggregate["NDCG@10"] > best_ndcg:
            best_label, best_ndcg = label, aggregate["NDCG@10"]
    print(f"→ 按 NDCG@10 最优：{best_label}")
    return best_label


def aggregate(per_query: list[dict]) -> dict:
    return {"R@20": mean([r["R@20"] for r in per_query]),
            "NDCG@10": mean([r["NDCG@10"] for r in per_query]),
            "P@5(g>=2)": mean([r["P@5(g>=2)"] for r in per_query]),
            "empty": sum(1 for r in per_query if r["empty"])}


def sweep_vector(cases) -> None:
    """向量网格：复用已保存的无阈值排序，纯本地重算，不调 API。"""
    if not RAW.exists():
        raise SystemExit(f"缺少 {RAW}，先跑一次向量排序采集")
    raw = {q: [(alias(c), cos) for c, cos in rows]
           for q, rows in json.loads(RAW.read_text(encoding="utf-8")).items()}
    rows = []
    for abs_cos, ratio in VECTOR_GRID:
        per_query = [score_of(apply_cut(raw[qid], abs_cos, ratio), rel)
                     for qid, rel in cases]
        rows.append((f"abs={abs_cos:.4f} ratio={ratio:.1f}", aggregate(per_query)))
    report("向量模式：绝对下限(cos) × 相对比例", rows)


def score_from_cos(cos: float) -> float:
    """把 cos 阈值换算成生产常量所在的 score 空间：score = 1/(3-2cos)。"""
    return 1.0 / (3 - 2 * cos)


async def sweep_hybrid(cases, abs_coss: list[float], ratio: float, limit: int,
                       reranker_kind: str) -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model=settings["siliconflow_embedding_model"])
    queries = json.loads((EVAL / "real_queries.json").read_text(encoding="utf-8"))
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    async with httpx.AsyncClient(timeout=90) as client:
        embedding = SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"])
        if reranker_kind == "local":
            # 远程 reranker 在限流时会静默退回未重排顺序，使同一参数两次运行的排名都不一致
            # （实测 top20 交集低至 0.84），因此参数扫描必须用确定性 reranker 才能比较。
            reranker = LocalKeywordReranker()
        else:
            reranker = SiliconFlowRerankerProvider(
                api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                model=settings["siliconflow_reranker_model"])
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=reranker, search_timeout=60)
        rows = []
        for abs_cos in abs_coss:
            # 生产常量在 score 空间比较（score >= VECTOR_MIN_SIMILARITY），这里统一按 cos
            # 标注网格，换算后再注入，避免两条扫描线的横轴口径不一致。
            service_module.VECTOR_MIN_SIMILARITY = score_from_cos(abs_cos)
            service_module.VECTOR_FUSION_MIN_SIMILARITY = score_from_cos(abs_cos)
            service_module.VECTOR_RELATIVE_RATIO = ratio
            path = OUT / f"real_hybrid@{reranker_kind}@cos{abs_cos:.4f}_ratio{ratio:.1f}.json"
            done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            pending = [q for q in queries if q["qid"] not in done]
            print(f"[hybrid {reranker_kind} abs={abs_cos:.4f}] 已完成={len(done)} "
                  f"待跑={len(pending)}", flush=True)
            degraded = 0
            for query in pending:
                page = await service.search(parse_query(query["text"]).keywords,
                                           CandidateFilters(), limit=limit, mode="hybrid")
                ids, seen = [], set()
                for hit in page.items:
                    key_id = alias(hit.candidate_id)
                    if key_id not in seen:
                        seen.add(key_id)
                        ids.append(key_id)
                done[query["qid"]] = {"intent": query.get("title") or "", "hybrid": ids,
                                      "degraded": list(page.degraded_reasons)}
                degraded += bool(page.degraded_reasons)
                path.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            per_query = []
            for qid, relevant in cases:
                item = done.get(qid) or {}
                per_query.append(score_of(list(item.get("hybrid") or []), relevant))
            aggregate_row = aggregate(per_query)
            aggregate_row["degraded"] = degraded
            rows.append((f"cos={abs_cos:.4f} ratio={ratio:.1f}", aggregate_row))
            print(f"[hybrid {reranker_kind} cos={abs_cos:.4f}] 本档降级={degraded}", flush=True)
        report(f"混合模式：绝对下限(cos) 扫描（reranker={reranker_kind}，相对比例固定）", rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("vector", "hybrid", "both"), default="both")
    parser.add_argument("--abss", default="0.6667,0.60,0.55",
                        help="hybrid 扫描的绝对下限（cos）列表")
    parser.add_argument("--ratio", type=float, default=0.9)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--reranker", choices=("local", "real"), default="local",
                        help="local=确定性（可复现，用于参数比较）；real=生产远程模型")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    cases = load_cases()
    print(f"有标签查询={len(cases)}")
    if args.mode in ("vector", "both"):
        sweep_vector(cases)
    if args.mode in ("hybrid", "both"):
        abss = [float(x) for x in args.abss.split(",") if x.strip()]
        asyncio.run(sweep_hybrid(cases, abss, args.ratio, args.limit, args.reranker))
    print()
    print("产物目录:", OUT)


if __name__ == "__main__":
    main()
