"""阶段 0：AI 语义改写的 A/B 评测走**生产** ``/api/search/candidates`` 入口。

与 ``rewrite_on_searches_2026_09_19.py``（直接调用 ``HybridSearchService``）的区别：
本脚本走完整 HTTP 链路，因此覆盖 ``parse_query`` 硬条件提取、表单条件合并、
``retained_keywords`` 与 hydration —— 这些正是直接调服务层时缺失的部分。

用法（需先启动 sidecar，拿到它的端口与会话 token）：

    py -3.12 scripts/rewrite_ab_via_api_2026_09_20.py \
        --base-url http://127.0.0.1:8765 --token <session-token> \
        --queries .tmp-judge/queries.json --output .tmp-plan/rewrite_ab_api.json

产物（脱敏：候选人 ID 一律 sha256 别名，不记录密钥与简历正文）：
- ``frozen``：查询集指纹、请求形状、服务端索引兼容性与待投影数（索引是否已冻结的证据）
- 每条查询 × 模式 × 改写开关的 top-10 别名序列、耗时、降级原因
- ``query_plan`` 的公开四值 ``rewrite_status`` + ``rewrite_applied`` +
  ``rewrite_fallback_reason`` 分布
- 改写开关前后的 top-10 Jaccard 与排序变化率
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path

import httpx

MODES = ("vector", "hybrid")
TOP_K = 10


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_queries(path: Path, phrasings: tuple[str, ...] = ("vague",)) -> list[tuple[str, str]]:
    """支持两种输入：``{J01: {"vague": "...", "standard": "..."}}`` 字典，或 ``[{"query": "..."}]`` 列表。

    字典里允许存在 ``_note`` 之类的说明性字符串键，因此只在值是 dict 且指定问法非空时才取用；
    取 ``vague,standard,colloquial`` 即 20 个 JD 的 60 条分层查询集。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        queries: list[tuple[str, str]] = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            for phrasing in phrasings:
                text = str(value.get(phrasing) or "").strip()
                if text:
                    queries.append((f"{key}:{phrasing}", text))
        return queries
    return [(str(index).zfill(2), str(item.get("query") or "")) for index, item in enumerate(payload, 1)]


def jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if (a | b) else 1.0


async def run_query(client: httpx.AsyncClient, query: str, mode: str, rewrite: bool) -> dict:
    started = time.monotonic()
    response = await client.post("/api/search/candidates", json={
        "query": query,
        "mode": mode,
        "rewrite_enabled": rewrite,
        "limit": TOP_K,
    })
    elapsed_ms = (time.monotonic() - started) * 1000
    response.raise_for_status()
    body = response.json()
    plan = body.get("query_plan") or {}
    return {
        "ids": [alias(item["candidate_id"]) for item in body.get("items", [])],
        "elapsed_ms": round(elapsed_ms, 1),
        "status": body.get("status"),
        "empty_reason": body.get("empty_reason"),
        "degraded": body.get("degraded_reasons") or [],
        "rewrite_status": plan.get("rewrite_status"),
        "rewrite_applied": plan.get("rewrite_applied"),
        "rewrite_fallback_reason": plan.get("rewrite_fallback_reason"),
        "semantic_query_chars": len(plan.get("semantic_query") or ""),
        "semantic_query_sha": (hashlib.sha256((plan.get("semantic_query") or "").encode("utf-8")).hexdigest()[:12]
                               if plan.get("semantic_query") else None),
    }


def _distribution(values: list) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * ratio) - 1)]


async def frozen_context(client: httpx.AsyncClient, queries_path: Path, queries: list[tuple[str, str]]) -> dict:
    """冻结口径：查询集指纹 + 请求形状 + 服务端索引状态（尽力而为）。

    阶段 0 要求「冻结数据库、索引、embedding 模型、reranker 模型和查询集」。走 HTTP 时
    客户端拿不到模型名，但可以记录服务端自报的索引兼容性与待投影数——那是「索引已冻结」
    最直接的证据；查询集哈希则用于证明调参集与最终验收集是两个不同文件。
    """
    raw = queries_path.read_bytes()
    context = {
        "api": {"endpoint": "/api/search/candidates", "modes": list(MODES), "top_k": TOP_K},
        "queries": {"path": str(queries_path),
                    "sha256": hashlib.sha256(raw).hexdigest()[:16],
                    "bytes": len(raw), "count": len(queries)},
    }
    try:
        response = await client.get("/api/search/index-status")
        response.raise_for_status()
        document = response.json()
        context["server_index"] = {
            "pending": document.get("pending"),
            "failed": document.get("failed"),
            "indexes": [
                {"entity_type": item.get("entity_type"), "compatible": item.get("compatible"),
                 "error": item.get("error")}
                for item in document.get("indexes") or []
            ],
            "rebuild": document.get("rebuild"),
        }
    except Exception as error:  # noqa: BLE001 - 状态端点不可用不该阻断评测
        context["server_index"] = {"unavailable": type(error).__name__}
    return context


def _judge_metrics(records: list[dict], judge_dir: Path) -> dict | None:
    """用与消融脚本同一套判决口径给改写开/关两臂打分；缺标注时返回 None。

    复用 ``retrieval_ablation_2026_09_20.py`` 里的实现（同目录直接按路径加载），
    避免两套 nDCG/命中率实现随时间漂移。
    """
    if not (judge_dir / "blinding_map.json").exists():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "retrieval_ablation_2026_09_20", Path(__file__).resolve().parent / "retrieval_ablation_2026_09_20.py")
    assert spec and spec.loader
    ablation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ablation)

    grades = ablation.load_grades(judge_dir)
    arms: dict[str, dict] = {"rewrite_off": {}, "rewrite_on": {}}
    for record in records:
        for arm in arms:
            arms[arm].setdefault(record["query_key"], {})[record["mode"]] = record[arm]["ids"]
    scored = {arm: ablation.score_arm(top, grades) for arm, top in arms.items()}
    return {
        "judge_dir": str(judge_dir),
        "baseline": "rewrite_off",
        "arms": {arm: summary for arm, (summary, _) in scored.items()},
        "vs_baseline": ablation.compare_arms(scored, "rewrite_off"),
        "note": "口径同 judge_eval_metrics_2026_09_19.py；本集为调参集，是否默认开启仍需独立验收集。",
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="改写 A/B（生产 API 入口）")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--phrasings", default="vague",
                        help="字典形状查询集取哪些问法，逗号分隔；vague,standard,colloquial 即 60 条分层集")
    parser.add_argument("--output", type=Path, default=Path(".tmp-plan/rewrite_ab_api.json"))
    parser.add_argument("--judge-dir", type=Path, default=Path(".tmp-judge"),
                        help="已判决标注目录；缺失则只输出排序差异与状态分布")
    parser.add_argument("--timeout", type=float, default=120.0)
    options = parser.parse_args()

    phrasings = tuple(part.strip() for part in options.phrasings.split(",") if part.strip())
    queries = load_queries(options.queries, phrasings)
    records: list[dict] = []
    latencies: list[float] = []
    jaccards: dict[str, list[float]] = {mode: [] for mode in MODES}

    async with httpx.AsyncClient(base_url=options.base_url, timeout=options.timeout,
                                 headers={"X-Kerui-Session": options.token}) as client:
        frozen = await frozen_context(client, options.queries, queries)
        print("冻结口径：", json.dumps(frozen, ensure_ascii=False))
        for key, text in queries:
            if not text.strip():
                continue
            for mode in MODES:
                off = await run_query(client, text, mode, rewrite=False)
                on = await run_query(client, text, mode, rewrite=True)
                latencies.extend([off["elapsed_ms"], on["elapsed_ms"]])
                jaccards[mode].append(jaccard(off["ids"], on["ids"]))
                records.append({"query_key": key, "mode": mode, "rewrite_off": off, "rewrite_on": on})
            print(f"[{key}] {text[:24]}", flush=True)

    metrics = _judge_metrics(records, options.judge_dir)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"frozen": frozen, "records": records}
    if metrics is not None:
        payload["metrics"] = metrics
    options.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    on_records = [record["rewrite_on"] for record in records]
    print("\n公开 rewrite_status 分布：", _distribution([r["rewrite_status"] for r in on_records]))
    print("rewrite_applied 比例：",
          f"{sum(1 for r in on_records if r['rewrite_applied'])}/{len(on_records)}")
    print("rewrite_fallback_reason 分布：",
          _distribution([r["rewrite_fallback_reason"] for r in on_records]))
    for mode in MODES:
        values = jaccards[mode]
        if values:
            print(f"{mode} top-{TOP_K} Jaccard：均值 {statistics.mean(values):.4f} "
                  f"排序变化率 {sum(1 for value in values if value < 1.0)}/{len(values)}")
    print(f"延迟 P50={statistics.median(latencies):.0f}ms P95={_percentile(latencies, 0.95):.0f}ms")
    if metrics is not None:
        print("判决式指标：", json.dumps(metrics, ensure_ascii=False))
    print(f"产物：{options.output}")


if __name__ == "__main__":
    asyncio.run(main())
