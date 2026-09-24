"""§5 库规模子集实验：参数（阈值裁剪率 / 命中数 / 边际收益）与库规模的相关性。

子集构造：按**候选人 ID** 的 sha256 前缀取模确定性抽样（不按 chunk，避免同一个人被拆散）：

    keep(c) = int(sha256(c)[:8], 16) % 8 < ratio * 8

比率 {1/8, 1/4, 1/2, 1/1}。抽样通过 ``CandidateFilters.candidate_ids`` 下推到索引层，
因此每个档位测的是「库里只有这些人的时候」的真实漏斗。

测量项（对应方案 §5.3）：
1. 每查询 top-50 中合格人数（绝对命中数，关注是否饱和）
2. 相对覆盖率：本档 top-50 有多少落在「全库 top-50」里
3. 边际收益：limit 20 → 50 → 100 的新增合格人数
4. 两条绝对阈值在各档的裁剪率（向量通道原始排序上直接统计）

安全：只读 `.dev-data`；不调用 `optimize_pending()`；候选人 ID 用 sha256 别名。
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
OUT = ROOT / ".tmp-plan" / "scale_subsets.json"
RATIOS = ((1, 8), (1, 4), (1, 2), (1, 1))
LIMITS = (20, 50, 100)


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def in_subset(candidate_id: str, numerator: int, denominator: int) -> bool:
    bucket = int(hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:8], 16) % denominator
    return bucket < numerator


def load_cases() -> list[tuple[str, str, dict]]:
    queries = json.loads((EVAL / "real_queries.json").read_text(encoding="utf-8"))
    labels = json.loads((EVAL / "real_labels.json").read_text(encoding="utf-8"))
    cases = []
    for query in queries:
        relevant = {cid: int(g) for cid, g in (labels.get(query["qid"]) or {}).items()}
        if relevant:
            cases.append((query["qid"], query["text"], relevant))
    return cases


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


def all_candidate_ids(index: LanceDBSearchIndex) -> list[str]:
    """从索引里取全部候选人 ID（只读）。"""
    table = index.database.open_table(index.table_name)
    arrow = table.search().select(["candidate_id"]).limit(None).to_arrow()
    return sorted(set(arrow.column("candidate_id").to_pylist()))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=int, default=40, help="用前 N 条带标签查询")
    parser.add_argument("--probe", action="store_true", help="只跑 1/8 档 1 条查询，验证可行性")
    args = parser.parse_args()

    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    cases = load_cases()[: args.queries]
    print(f"带标签查询={len(cases)}")

    started = time.monotonic()
    ids = all_candidate_ids(index)
    print(f"索引内候选人数={len(ids)}（取用 {time.monotonic() - started:.1f}s）")

    subsets = {
        f"{num}/{den}": [c for c in ids if in_subset(c, num, den)]
        for num, den in RATIOS
    }
    for name, members in subsets.items():
        print(f"  子集 {name:<4} 人数={len(members)}")
    del ids

    if args.probe:
        async with httpx.AsyncClient(timeout=90) as client:
            embedding = CachingEmbedding(SiliconFlowEmbeddingProvider(
                api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                model=settings["siliconflow_embedding_model"]))
            service = HybridSearchService(index=index, embedding_provider=embedding,
                                          reranker_provider=LocalKeywordReranker(),
                                          search_timeout=90)
            qid, text, relevant = cases[0]
            for name in ("1/8", "1/1"):
                t0 = time.monotonic()
                page = await service.search(
                    text, CandidateFilters(candidate_ids=tuple(subsets[name])), limit=50,
                    mode="hybrid")
                print(f"  probe {name}: 返回={len(page.items)} "
                      f"降级={list(page.degraded_reasons)} 用时={time.monotonic() - t0:.2f}s "
                      f"R@50={recall_at_k([alias(h.candidate_id) for h in page.items], relevant, 50)}")
        return

    report: dict = {"候选人数": {k: len(v) for k, v in subsets.items()}, "档位": {}}
    async with httpx.AsyncClient(timeout=120) as client:
        embedding = CachingEmbedding(SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"]))
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=LocalKeywordReranker(),
                                      search_timeout=120)

        top50_by_query_by_ratio: dict[str, dict[str, list[str]]] = {}
        for name, members in subsets.items():
            top50_by_query: dict[str, list[str]] = {}
            row: dict = {}
            for limit in LIMITS:
                r20, r50, ndcg, hits, empty, elapsed = [], [], [], [], 0, []
                for qid, text, relevant in cases:
                    t0 = time.monotonic()
                    page = await service.search(
                        text, CandidateFilters(candidate_ids=tuple(members)), limit=limit,
                        mode="hybrid")
                    elapsed.append((time.monotonic() - t0) * 1000)
                    ranked, seen = [], set()
                    for hit in page.items:
                        key_id = alias(hit.candidate_id)
                        if key_id not in seen:
                            seen.add(key_id)
                            ranked.append(key_id)
                    if not ranked:
                        empty += 1
                        continue
                    if limit == 50:
                        top50_by_query[qid] = ranked[:50]
                    value20 = recall_at_k(ranked, relevant, 20)
                    if value20 is None:
                        continue
                    r20.append(value20)
                    r50.append(recall_at_k(ranked, relevant, 50))
                    ndcg.append(ndcg_at_k(ranked, relevant, 10))
                    hits.append(sum(1 for c in ranked[:50] if relevant.get(c, 0) >= 2))
                row[f"limit{limit}"] = {
                    "R@20": round(sum(r20) / len(r20), 4) if r20 else 0.0,
                    "R@50": round(sum(r50) / len(r50), 4) if r50 else 0.0,
                    "NDCG@10": round(sum(v for v in ndcg if v is not None) /
                                     max(1, len([v for v in ndcg if v is not None])), 4),
                    "top50合格人数均值": round(sum(hits) / len(hits), 2) if hits else 0.0,
                    "空结果": empty,
                    "P50ms": round(sorted(elapsed)[len(elapsed) // 2], 1),
                }
                print(f"  {name:<4} limit={limit:<4} {row[f'limit{limit}']}", flush=True)
            top50_by_query_by_ratio[name] = top50_by_query
            report["档位"][name] = row

        # 相对覆盖率：各档 top-50 有多少落在全库（1/1）top-50 里。
        reference = top50_by_query_by_ratio.get("1/1") or {}
        for name, row in report["档位"].items():
            if name == "1/1" or not reference:
                continue
            covered = []
            for qid, ranked in top50_by_query_by_ratio.get(name, {}).items():
                base = set(reference.get(qid, []))
                if base:
                    covered.append(len(set(ranked) & base) / len(base))
            row["相对覆盖率(占全库top50)"] = round(sum(covered) / len(covered), 4) if covered else None

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 库规模 × 漏斗 ===")
    print(f"{'子集':<6}{'人数':>7}{'limit':>7}{'R@20':>8}{'R@50':>8}{'NDCG@10':>9}"
          f"{'top50合格':>11}{'空':>4}{'P50ms':>9}{'相对覆盖率':>11}")
    for name, row in report["档位"].items():
        for limit in LIMITS:
            cell = row[f"limit{limit}"]
            cover = row.get("相对覆盖率(占全库top50)", "") if limit == 50 else ""
            print(f"{name:<6}{report['候选人数'][name]:>7}{limit:>7}{cell['R@20']:>8.4f}"
                  f"{cell['R@50']:>8.4f}{cell['NDCG@10']:>9.4f}{cell['top50合格人数均值']:>11.2f}"
                  f"{cell['空结果']:>4}{cell['P50ms']:>9.1f}{str(cover):>11}")
    print(f"\n查询向量 API 调用={embedding.calls}  产物={OUT}")


if __name__ == "__main__":
    asyncio.run(main())
