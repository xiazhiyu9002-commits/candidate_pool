"""纯向量阈值消融：在冻结索引上记录阈值前原始相似度，网格对比阈值组合。

只读冻结快照与索引，不写生产索引。用同一盲标集计算 P@5（相关=grade>=2，分母固定 5）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
SNAPSHOT = ROOT / ".semantic-audit-snapshot"

# 待消融的绝对相似度下限与相对比率。
ABS_THRESHOLDS = (0.45, 0.50, 0.55, 0.60)
REL_RATIOS = (0.85, 0.90, 1.00)


def alias(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def p_at_k(ids: list[str], labels: dict, k: int = 5) -> float:
    grades = [labels.get(cid, {}).get("grade", 0) for cid in ids[:k]]
    return sum(1 for g in grades if g >= 2) / k


def apply_threshold(scored: list[tuple[str, float]], abs_t: float, rel: float) -> list[tuple[str, float]]:
    if not scored:
        return []
    top = scored[0][1]
    threshold = max(abs_t, top * rel)
    return [(cid, s) for cid, s in scored if s >= threshold]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    args = parser.parse_args()

    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
    from kerui_recruit.search.contracts import CandidateFilters
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    retrieval = json.loads((args.snapshot / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    labels = json.loads((args.snapshot / "blind_labels.json").read_text(encoding="utf-8"))

    index = LanceDBSearchIndex(args.snapshot / "search", vector_dimension=1024,
                               embedding_model=settings["siliconflow_embedding_model"])
    index.warmup()

    raw: dict[str, list[tuple[str, float]]] = {}
    async with httpx.AsyncClient(timeout=60) as client:
        embedding = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        for qid, item in retrieval.items():
            if qid > "Q24":
                continue
            query = item["intent"]
            vector = await embedding.embed_query(query)
            rows = index.search_vector(tuple(vector), CandidateFilters(), 100)
            scored = [(alias(r["candidate_id"]), round(1.0 / (1.0 + r["_distance"]), 6))
                      for r in rows if isinstance(r.get("_distance"), (int, float))]
            raw[qid] = scored
            print(f"{qid}: top1={scored[0][1] if scored else None} raw_count={len(scored)}", flush=True)

    # 网格对比
    print("\n=== 纯向量阈值网格（宏平均 P@5，相关=grade>=2）===")
    print("abs\\rel", *[str(r) for r in REL_RATIOS], sep="\t")
    best = None
    for abs_t in ABS_THRESHOLDS:
        row = [f"{abs_t}"]
        for rel in REL_RATIOS:
            p5s = []
            empties = 0
            for qid, scored in raw.items():
                kept = apply_threshold(scored, abs_t, rel)
                ids = [cid for cid, _ in kept]
                p5s.append(p_at_k(ids, labels.get(qid, {})))
                if not ids:
                    empties += 1
            macro = round(sum(p5s) / len(p5s), 4) if p5s else None
            row.append(f"{macro}(e{empties})")
            if best is None or (macro is not None and macro > best[0]):
                best = (macro, abs_t, rel, empties)
        print("\t".join(row))
    print(f"\n最优组合: P@5={best[0]} abs={best[1]} rel={best[2]} 空结果={best[3]}")

    # 当前生产阈值（0.60 / 0.90）下的空结果查询
    print("\n=== 当前阈值(0.60/0.90)下各查询返回数 ===")
    for qid, scored in raw.items():
        kept = apply_threshold(scored, 0.60, 0.90)
        flag = "  <-- 空" if not kept else ""
        top = scored[0][1] if scored else None
        print(f"{qid}: kept={len(kept)} top1={top}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
