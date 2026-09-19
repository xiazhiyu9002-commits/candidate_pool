"""标定向量相似度下限。

对一批真实/构造查询跑向量检索，输出每个查询的 top-K score 分布，
供人工确定 service.VECTOR_MIN_SIMILARITY（score = 1/(1+distance)，bge-m3 归一化后约 [0.33, 1]）。

用法：
  SILICONFLOW_API_KEY=... python -m kerui_recruit.bench.calibrate_vector_threshold \
      --index-root .dev-data/search
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path

import httpx

from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


QUERIES: list[str] = [
    "Java 后端工程师",
    "高并发系统设计",
    "金融风控",
    "大模型 RAG 应用",
    "Python 数据分析",
    "Flink 实时计算",
    "微服务架构",
    "支付清结算",
    "Kubernetes 云原生",
    "前端 React",
    "机器学习算法工程师",
    "数据仓库 ETL",
    "DevOps CI/CD",
    "人工智能产品经理",
    "测试开发",
    "想找做过推荐系统的人",
    "熟悉 Spring Cloud 的资深工程师",
    "银行核心系统开发",
    "向量数据库",
    "能做大模型落地的人",
]


async def _run(index_root: Path) -> list[dict]:
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY 未设置")
    index = LanceDBSearchIndex(index_root, vector_dimension=1024, embedding_model="BAAI/bge-m3")
    if not index.is_ready():
        raise RuntimeError("索引未就绪或不兼容")
    report: list[dict] = []
    async with httpx.AsyncClient() as client:
        provider = SiliconFlowEmbeddingProvider(api_key=api_key, client=client)
        for query in QUERIES:
            vector = await provider.embed_query(query)
            rows = index.search_vector(tuple(vector), CandidateFilters(), 20)
            hits = index.hits_from_rows(rows, "vector")
            scores = [hit.score for hit in hits]
            report.append({
                "query": query,
                "count": len(scores),
                "top1": round(scores[0], 4) if scores else None,
                "top5": round(scores[4], 4) if len(scores) > 4 else None,
                "median": round(statistics.median(scores), 4) if scores else None,
                "tail": round(scores[-1], 4) if scores else None,
            })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(_run(args.index_root))
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False))
        return 1
    all_top1 = [r["top1"] for r in report if r["top1"] is not None]
    all_tail = [r["tail"] for r in report if r["tail"] is not None]
    print(json.dumps({
        "queries": report,
        "summary": {
            "min_top1": round(min(all_top1), 4) if all_top1 else None,
            "max_top1": round(max(all_top1), 4) if all_top1 else None,
            "min_tail": round(min(all_tail), 4) if all_tail else None,
            "suggested_threshold_hint": "阈值应 ≤ 大多数查询的 top1，且能压掉明显弱相关的 tail；"
                                        "建议取「top1 最小值」与「tail 中位数」之间，人工确认后写入 service.VECTOR_MIN_SIMILARITY",
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
