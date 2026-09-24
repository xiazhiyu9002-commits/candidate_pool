"""阶段 1 定档 A/B：混合模式 × {检索正文 开/关} × {弱命中过滤 开/关}。

设计文档 `docs/superpowers/specs/2026-09-21-query-parse-hybrid-body-design.md`
§混合正文检索 第 4 条的验收要求：用现有 bench 对这四个组合做 A/B，并按
**nDCG@10 降幅 ≤ 0.01、R@20 不降、空结果率不升、p95 延迟增幅 < 20%** 判定
阶段 1（前端解禁 + 混合 FTS 弱命中过滤）能否成立。

四条臂：

| 臂 | `search_body` | `HYBRID_WEAK_HIT_FILTER` | 含义 |
| --- | --- | --- | --- |
| `body0_weak0` | 关 | 关 | 阶段 1 **之前**的现网行为（基线） |
| `body0_weak1` | 关 | 开 | 只加弱过滤（正文仍关） |
| `body1_weak0` | 开 | 关 | 正文解禁但**没有**弱过滤（若不达标就是噪声证据） |
| `body1_weak1` | 开 | 开 | 用户打开正文开关时的**上线形态** |

四条臂都用同一个 `limit=20` 与同一组查询，保证「请求 limit 改变内部候选池」（H5）不干扰比较。
取数口径必须与生产一致：`api/search.py` 传给检索的是规则解析产出的
`keywords` + `concepts` + `filters`，而不是原始输入串——只传原串会让概念闸门拿到空 `concepts`，
「弱过滤臂」退化成空壳（实测 `queries_with_concept_gate=0`），A/B 结论不成立。
指标口径与 `scripts/retrieval_ablation_2026_09_20.py` 一致（判决式标注：nDCG gain = 2^grade − 1，
强相关 = grade ≥ 2），直接复用其取数函数，避免第二套实现。

用法：

    # 调参集（分层问法，20 JD × 3 问法）
    py -3.12 scripts/stage1_body_weak_ab_2026_09_21.py \
        --queries .tmp-judge/queries.json --phrasings vague,standard,colloquial \
        --output .tmp-ablation/stage1-body-weak.json

    # 独立验收集（99 条真实 match 查询 + 判决式标签）
    py -3.12 scripts/stage1_body_weak_ab_2026_09_21.py \
        --queries .tmp-real-eval/real_queries.json --labels .tmp-real-eval/real_labels.json \
        --output .tmp-ablation/stage1-body-weak-real.json

安全：只读 `.dev-data`；不打印密钥；候选人 ID 一律 sha256 别名。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DEV = ROOT / ".dev-data"
# 四条臂 = {正文, 弱过滤} 的笛卡尔积；(False, False) 是阶段 1 之前的现网行为。
ARMS: tuple[tuple[str, bool, bool], ...] = (
    ("body0_weak0", False, False),
    ("body0_weak1", False, True),
    ("body1_weak0", True, False),
    ("body1_weak1", True, True),
)
BASELINE = "body0_weak0"


def _load_ablation():
    """复用既有消融脚本的取数与指标函数（只读、无副作用）；只加载一次。"""
    global _ABLATION
    if _ABLATION is None:
        path = ROOT / "scripts" / "retrieval_ablation_2026_09_20.py"
        spec = importlib.util.spec_from_file_location("retrieval_ablation_2026_09_20", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _ABLATION = module
    return _ABLATION


_ABLATION = None


def load_queries(path: Path, phrasings: tuple[str, ...]) -> list[tuple[str, str]]:
    """与 `parse_quality_2026_09_21.py` 同口径读两种查询集形状。

    既有消融脚本的列表分支只认 `query` 键、且用序号当 qid，而独立验收集
    `.tmp-real-eval/real_queries.json` 用 `qid` + `text`——标签按 qid 对齐，序号会让全部对不上。
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
    queries = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("query") or "").strip()
        if text:
            queries.append((str(item.get("qid") or f"Q{index:03d}"), text))
    return queries


async def run_arms(queries: list[tuple[str, str]], arms, *, limit: int) -> dict:
    import httpx

    from kerui_recruit.search import service as service_module
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.observer import InMemorySearchObserver
    from kerui_recruit.search.query import parse_query
    from kerui_recruit.search.service import HybridSearchService

    ab = _load_ablation()
    settings, key = ab.settings_and_keys()
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    # 必须走上生产同一口径：api/search.py 传的是「规则解析出的 keywords + concepts + filters」，
    # 只传原串会让概念闸门拿到空 concepts（实测 queries_with_concept_gate=0），
    # 弱过滤臂就变成空壳，A/B 结论无效。
    plans = {qid: parse_query(text) for qid, text in queries if text.strip()}

    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=600) as client:
        embedding, reranker = ab.providers(settings, key, client)
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=reranker, search_timeout=600)
        for name, search_body, weak_filter in arms:
            service_module.HYBRID_WEAK_HIT_FILTER = weak_filter
            observer = InMemorySearchObserver()
            top: dict[str, list[str]] = {}
            latencies: list[float] = []
            empty = 0
            started = time.monotonic()
            for qid, plan in plans.items():
                begin = time.monotonic()
                page = await service.search(plan.keywords, plan.filters, limit=limit, mode="hybrid",
                                            search_body=search_body, operator="smart",
                                            concepts=plan.concepts, observer=observer)
                latencies.append(round((time.monotonic() - begin) * 1000, 1))
                if not page.items:
                    empty += 1
                top[qid] = [ab.alias(hit.candidate_id) for hit in page.items[:limit]]
            results[name] = {
                "top": top, "latency_ms": latencies,
                "latency_summary": {"p50": statistics.median(latencies) if latencies else 0.0,
                                    "p95": ab.percentile(latencies, 0.95)},
                "empty_result_queries": empty,
                "empty_result_rate": round(empty / len(top), 4) if top else 0.0,
                "queries": len(top),
                "search_body": search_body,
                "hybrid_weak_hit_filter": weak_filter,
                "fts_filter_counts": _filter_counts(observer),
            }
            print(f"[{name}] body={int(search_body)} weak={int(weak_filter)} "
                  f"{time.monotonic() - started:.0f}s", flush=True)

    # 恢复现网默认，避免同进程后续调用误用对照臂。
    service_module.HYBRID_WEAK_HIT_FILTER = True
    return results


def _filter_counts(observer) -> dict:
    """弱过滤两道闸门在各查询上的累计行数（证明过滤真的发生/未发生）。"""
    before = after_terms = after_concept = 0
    gated = emptied = 0
    for payload in observer.diagnostics:
        before += int(payload.get("hybrid_fts_rows_before_filter") or 0)
        after_terms += int(payload.get("hybrid_fts_rows_after_hit_terms") or 0)
        after_concept += int(payload.get("hybrid_fts_rows_after_concept") or 0)
        gated += 1 if payload.get("hybrid_fts_concept_gate") else 0
        emptied += 1 if payload.get("hybrid_fts_gate_emptied") else 0
    return {"rows_before": before, "rows_after_hit_terms": after_terms,
            "rows_after_concept": after_concept, "queries_with_concept_gate": gated,
            "queries_gate_failed_open": emptied}


def load_grades(judge_dir: Path) -> dict[str, dict[str, int]]:
    """读判决式标签。两种文件名都要收：扩样集的新 JD 落在 `judge_out_new_batch*.json`。

    既有 `retrieval_ablation_2026_09_20.load_grades` 只 glob `judge_out_batch*.json`，
    直接复用它会让扩充集的 16 个新 JD 静默无标签（实测 `labeled_queries` 从 108 掉到 60）。
    """
    ab = _load_ablation()
    found = list(judge_dir.glob("judge_out_batch*.json")) + \
        list(judge_dir.glob("judge_out_new_batch*.json"))
    if not found:
        return ab.load_grades(judge_dir)
    blinding = json.loads((judge_dir / "blinding_map.json").read_text(encoding="utf-8"))
    judgements: dict[str, dict[str, dict]] = {}
    for path in sorted(found):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for jid, body in payload.items():
            judgements[jid] = {item["label"]: item for item in body["judgements"]}
    grades: dict[str, dict[str, int]] = {}
    for jid, info in blinding.items():
        table = judgements.get(jid, {})
        grades[jid] = {candidate: int(table[letter]["grade"])
                       for letter, candidate in (info.get("letters") or {}).items()
                       if letter in table}
    return grades


def attach_metrics(results: dict[str, dict], judge_dir: Path, labels_path: Path | None) -> dict | None:
    """按判决式标注算 nDCG@10 / R@20；缺标注返回 None。

    两种标注来源：
    - ``--labels``：``{qid: {别名: grade}}``（独立验收集 `.tmp-real-eval/real_labels.json`）；
    - ``--judge-dir``：分层问法集的判决产物（blinding_map + judge_out_*batch*）。
    """
    ab = _load_ablation()
    if labels_path is not None and labels_path.exists():
        grades = json.loads(labels_path.read_text(encoding="utf-8"))
        source = str(labels_path)
    elif (judge_dir / "blinding_map.json").exists():
        grades = load_grades(judge_dir)
        source = str(judge_dir)
    else:
        return None
    arms: dict[str, dict] = {}
    per_query: dict[str, dict[str, float | None]] = {}
    for name, payload in results.items():
        rows: dict[str, dict[str, float | None]] = {}
        for qid, seq in (payload.get("top") or {}).items():
            table = grades.get(qid.split(":", 1)[0])
            if not table:
                continue
            rows[qid] = {
                "ndcg@10": ab.ndcg_at(seq, table, k=10),
                "recall@20": ab.strong_recall_at(seq, table, k=20),
            }
        per_query[name] = rows
        arms[name] = {
            "ndcg@10": _mean([item["ndcg@10"] for item in rows.values()]),
            "recall@20": _mean([item["recall@20"] for item in rows.values()]),
            "labeled_queries": len(rows),
            "empty_result_rate": payload.get("empty_result_rate"),
            "p50_ms": payload.get("latency_summary", {}).get("p50"),
            "p95_ms": payload.get("latency_summary", {}).get("p95"),
        }
    return {"label_source": source, "baseline": BASELINE, "arms": arms, "per_query": per_query}


def _mean(values) -> float | None:
    kept = [value for value in values if value is not None]
    return round(statistics.mean(kept), 4) if kept else None


def judge_gates(metrics: dict | None) -> dict:
    """按文档 §评测与门槛 的阶段 1 四条门槛逐条判定。"""
    if metrics is None:
        return {"verdict": "unlabeled", "reason": "缺判决式标注，无法判定 nDCG / R@20"}
    base = metrics["arms"][BASELINE]
    checks: dict[str, dict] = {}
    for name, arm in metrics["arms"].items():
        if name == BASELINE:
            continue
        paired = _paired(metrics["per_query"], name)
        ndcg_delta = _delta(arm["ndcg@10"], base["ndcg@10"])
        recall_delta = _delta(arm["recall@20"], base["recall@20"])
        latency_ratio = (arm["p95_ms"] / base["p95_ms"]) if base["p95_ms"] else None
        checks[name] = {
            "ndcg10_delta": ndcg_delta,
            "ndcg10_pass": ndcg_delta is not None and ndcg_delta >= -0.01,
            "recall20_delta": recall_delta,
            "recall20_pass": recall_delta is not None and recall_delta >= 0,
            "empty_result_rate": [base["empty_result_rate"], arm["empty_result_rate"]],
            "empty_rate_pass": arm["empty_result_rate"] <= base["empty_result_rate"],
            "p95_latency_ratio": round(latency_ratio, 4) if latency_ratio else None,
            "p95_pass": latency_ratio is not None and latency_ratio < 1.20,
            "ndcg10_paired_wins_losses_ties": paired,
        }
        checks[name]["all_pass"] = all(checks[name][key] for key in
                                       ("ndcg10_pass", "recall20_pass", "empty_rate_pass", "p95_pass"))
    return {"baseline": BASELINE, "arms": checks,
            "shipping_arms": {name: checks[name]["all_pass"]
                              for name in ("body0_weak1", "body1_weak1") if name in checks}}


def _delta(value, reference) -> float | None:
    if value is None or reference is None:
        return None
    return round(value - reference, 4)


def _paired(per_query: dict, name: str) -> dict:
    """逐查询配对胜负平（只比 nDCG@10，判断均值差是否被少数查询主导）。"""
    left = per_query.get(name) or {}
    right = per_query.get(BASELINE) or {}
    wins = losses = ties = 0
    for qid, entry in left.items():
        other = right.get(qid)
        if not other:
            continue
        a, b = entry.get("ndcg@10"), other.get("ndcg@10")
        if a is None or b is None:
            continue
        if a > b + 1e-9:
            wins += 1
        elif a < b - 1e-9:
            losses += 1
        else:
            ties += 1
    return {"wins": wins, "losses": losses, "ties": ties}


async def main() -> None:
    parser = argparse.ArgumentParser(description="阶段 1 正文×弱过滤 A/B 定档")
    parser.add_argument("--queries", type=Path, default=ROOT / ".tmp-judge" / "queries.json")
    parser.add_argument("--phrasings", default="vague,standard,colloquial")
    parser.add_argument("--limit", type=int, default=20, help="nDCG@10 取前 10、R@20 取前 20")
    parser.add_argument("--judge-dir", type=Path, default=ROOT / ".tmp-judge")
    parser.add_argument("--labels", type=Path, default=None,
                        help="独立验收集标注 {qid: {别名: grade}}；给了就优先用它")
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp-ablation" / "stage1-body-weak.json")
    parser.add_argument("--recount", type=Path, default=None,
                        help="离线重算已跑报告的指标与门槛（不调模型、不检索），用于修好取数后回填")
    options = parser.parse_args()

    if options.recount is not None:
        report = json.loads(options.recount.read_text(encoding="utf-8"))
        metrics = attach_metrics(report["results"], options.judge_dir, options.labels)
        report["metrics"] = metrics
        report["gates"] = judge_gates(metrics)
        options.recount.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("离线重算完成：", json.dumps(metrics["arms"] if metrics else {}, ensure_ascii=False))
        print("门槛：", json.dumps(report["gates"], ensure_ascii=False))
        return

    ab = _load_ablation()
    phrasings = tuple(part.strip() for part in options.phrasings.split(",") if part.strip())
    queries = load_queries(options.queries, phrasings)
    if not queries:
        raise SystemExit(f"查询集为空：{options.queries}")
    print(f"查询集={options.queries.name} 条数={len(queries)} limit={options.limit}", flush=True)
    frozen = ab.frozen_context(options.queries, queries)
    print("冻结口径：", json.dumps(frozen, ensure_ascii=False), flush=True)

    results = await run_arms(queries, ARMS, limit=options.limit)
    metrics = attach_metrics(results, options.judge_dir, options.labels)
    gates = judge_gates(metrics)
    report = {"frozen": frozen, "arms": [name for name, _, _ in ARMS], "limit": options.limit,
              "results": results, "metrics": metrics, "gates": gates}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("指标：", json.dumps(metrics["arms"] if metrics else {}, ensure_ascii=False))
    print("门槛：", json.dumps(gates, ensure_ascii=False))
    print(f"产物：{options.output}")


if __name__ == "__main__":
    asyncio.run(main())
