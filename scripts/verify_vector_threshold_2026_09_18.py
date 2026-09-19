"""在隔离副本上验证 `VECTOR_MIN_SIMILARITY` 标定对检索质量的影响。

背景：标定脚本已证明真实 BGE-M3 的 top1 相似度中位数仅 ~0.586（base），
而生产阈值是 ``max(0.6, top1*0.9)`` → 纯向量通道 15/24 查询被清空，混合模式
实际退化为 FTS+reranker。本脚本回答：把绝对下限降到实测分位后，混合/向量
两层指标是否真的改善。

做法（不修改生产代码、不写 ``.dev-data``）：
- 复用 ``.tmp-prefix-eval/index/{base,ai15}``（B2 已建好的隔离索引），不重建、不重嵌文档；
- 用 monkeypatch 改 ``kerui_recruit.search.service`` 的模块级阈值常量（调用时读取，安全）；
- 标签池与分母**冻结**为既有 3 个变体文件构成的池，保证与已发布数字可比；
- 每策略 × 每变体 × 每模式落盘 top100，输出两层指标对比表。

产出：``.tmp-prefix-eval/retrieval_threshold/*.json``、``.tmp-prefix-eval/threshold_verification.json``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
OUT_DIR = EVAL_DIR / "retrieval_threshold"
VARIANTS = ("base", "ai15")
MODES = ("vector", "hybrid")
LIMIT = 100

# 冻结分母：与已发布的 summary.json 使用同一批变体构成标签池。
FROZEN_POOL_FILES = ("base.json", "ai15.json", "rerank_prefixless.json")

# (策略名, 绝对下限, 相对比例)。生产当前为 ("current_0.60", 0.6, 0.9)。
POLICIES = (
    ("current_0.60", 0.60, 0.9),
    ("floor_0.50", 0.50, 0.9),
    ("floor_0.45", 0.45, 0.9),
    ("relative_only_0.40", 0.40, 0.9),
)
QUERY_CLASS = {
    "Q21": "hard", "Q22": "hard", "Q23": "hard",
    **{f"Q{i:02d}": "soft" for i in range(1, 21)},
    "Q24": "soft",
}
LABELED = tuple(sorted(QUERY_CLASS))
K_RECALL, K_RANK, K_PRECISION, K_MRR = 20, 10, 5, 20


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mean(values):
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def frozen_pool(labels: dict) -> dict[str, list[str]]:
    pool: dict[str, list[str]] = {}
    variants = [json.loads((EVAL_DIR / "retrieval" / name).read_text(encoding="utf-8"))
                for name in FROZEN_POOL_FILES]
    for qid in LABELED:
        ids, seen = [], set()
        for variant in variants:
            for ranked in variant.get(qid, {}).values():
                if not isinstance(ranked, list):
                    continue
                for cid in ranked:
                    if cid not in seen:
                        seen.add(cid)
                        ids.append(cid)
        for cid in labels.get(qid, {}):
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
        pool[qid] = ids
    return pool


def score(ranked: dict, cases: list[dict], subset: tuple[str, ...], pool: dict) -> dict:
    from kerui_recruit.evaluation.retrieval import (
        coverage_at_k, mrr_at_k, ndcg_at_k, recall_at_k,
    )

    filtered = [c for c in cases if c["id"] in subset]
    ranked_sub = {c["id"]: ranked.get(c["id"], []) for c in filtered}
    golden = {c["id"]: {cid for cid, g in c["relevant"].items() if g >= 2} for c in filtered}

    def extra(k: int) -> int:
        """top-k 中落在冻结池外的候选数（无标签，被计为不相关）。"""
        return sum(1 for c in filtered for cid in ranked.get(c["id"], [])[:k] if cid not in pool[c["id"]])

    return {
        "mean_returned": _mean([float(len(ranked.get(c["id"], []))) for c in filtered]),
        "empty_queries": sum(1 for c in filtered if not ranked.get(c["id"])),
        "extra_top10": extra(K_RANK), "extra_top20": extra(K_RECALL),
        "recall20": _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL) for c in filtered]),
        "recall20_hard": _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL)
                                for c in filtered if QUERY_CLASS[c["id"]] == "hard"]),
        "recall20_soft": _mean([recall_at_k(ranked.get(c["id"], []), golden[c["id"]], K_RECALL)
                                for c in filtered if QUERY_CLASS[c["id"]] == "soft"]),
        "coverage20": coverage_at_k(filtered, ranked_sub, K_RECALL),
        "ndcg10": _mean([ndcg_at_k(ranked.get(c["id"], []), c["relevant"], K_RANK) for c in filtered]),
        "mrr20": _mean([mrr_at_k(ranked.get(c["id"], []), c["relevant"], K_MRR) for c in filtered]),
        "p5": _mean([(sum(1 for cid in ranked.get(c["id"], [])[:K_PRECISION]
                          if c["relevant"].get(cid, 0) >= 2) / K_PRECISION)
                     if ranked.get(c["id"]) else None for c in filtered]),
    }


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider, SiliconFlowRerankerProvider
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search import service as service_module
    from kerui_recruit.search.service import HybridSearchService

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    intents = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    queries = {qid: item["intent"] for qid, item in intents.items() if qid <= "Q24"}
    labels = json.loads((EVAL_DIR / "blind_labels_ext.json").read_text(encoding="utf-8"))
    pool = frozen_pool(labels)
    cases = [{"id": qid, "relevant": {cid: int(labels[qid][cid]["grade"])
                                      for cid in pool[qid] if cid in labels.get(qid, {})}}
             for qid in LABELED]
    print(f"queries={len(queries)} pool={sum(len(v) for v in pool.values())}", flush=True)

    indexes = {name: LanceDBSearchIndex(EVAL_DIR / "index" / name, vector_dimension=1024,
                                        embedding_model=settings["siliconflow_embedding_model"])
               for name in VARIANTS}
    filters = CandidateFilters()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows: dict[str, dict] = {}
    unlabeled_hits: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=180) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])

        for policy, floor, ratio in POLICIES:
            service_module.VECTOR_MIN_SIMILARITY = floor
            service_module.VECTOR_RELATIVE_RATIO = ratio
            for name in VARIANTS:
                path = OUT_DIR / f"{name}.{policy}.json"
                if path.exists():
                    result = json.loads(path.read_text(encoding="utf-8"))
                else:
                    service = HybridSearchService(index=indexes[name], embedding_provider=emb,
                                                  reranker_provider=reranker, search_timeout=30)
                    result = {}
                    for qid, intent in queries.items():
                        entry: dict = {"intent": intent}
                        for mode in MODES:
                            page = await service.search(intent, filters, limit=LIMIT, mode=mode)
                            entry[mode] = [alias(hit.candidate_id) for hit in page.items]
                        result[qid] = entry
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                for mode in MODES:
                    ranked = {qid: list(entry.get(mode) or []) for qid, entry in result.items()}
                    rows[f"{name}.{mode} @ {policy}"] = score(ranked, cases, LABELED, pool)
                    unlabeled_hits[f"{name}.{mode} @ {policy}"] = sum(
                        1 for qid in LABELED for cid in ranked.get(qid, []) if cid not in pool[qid])
                print(f"  {name} {policy}: done", flush=True)

    service_module.VECTOR_MIN_SIMILARITY, service_module.VECTOR_RELATIVE_RATIO = 0.6, 0.9

    report = {"definition": "frozen pool (base/ai15/rerank_prefixless top100 union + prior labels); "
                             "extra = top100 items outside the frozen pool (unlabeled -> counted irrelevant)",
              "policies": [{"name": n, "floor": f, "ratio": r} for n, f, r in POLICIES],
              "metrics": rows, "extra_out_of_pool": unlabeled_hits}
    (EVAL_DIR / "threshold_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    keys = ("recall20", "recall20_soft", "recall20_hard", "coverage20", "ndcg10", "mrr20", "p5")
    header = (f"{'run':<34}" + "".join(f"{k:>11}" for k in keys)
              + f"{'ret':>7}{'empty':>7}{'x@10':>7}{'x@20':>7}{'xtot':>7}")
    print("\n" + header)
    print("-" * len(header))
    for label, row in rows.items():
        line = f"{label:<34}" + "".join(f"{row[k]:>11.4f}" for k in keys)
        line += (f"{row['mean_returned']:>7.1f}{row['empty_queries']:>7}"
                 f"{row['extra_top10']:>7}{row['extra_top20']:>7}{unlabeled_hits[label]:>7}")
        print(line)
    print("\nwritten: .tmp-prefix-eval/threshold_verification.json")


if __name__ == "__main__":
    asyncio.run(main())
