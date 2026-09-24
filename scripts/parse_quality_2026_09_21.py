"""阶段 3 解析质量抽检：在真实库（.dev-data）上跑「解析开启 / 关闭」对照。

用途：给「阶段 3 字段启用」与「阶段 5 是否默认开启解析」提供可复算的验收证据。
本脚本只读直连 ``.dev-data`` 的索引与数据库，**不写任何生产数据**，产物全部为别名 ID。

用法：

    # 只验证接线与输入（不调模型、不检索）
    py -3.12 scripts/parse_quality_2026_09_21.py --dry-run

    # 正式抽检（会真实调用解析模型与向量模型）
    py -3.12 scripts/parse_quality_2026_09_21.py --max 40 --limit 50

输入（``--queries``）支持两种形状：
- ``[{qid, text}, ...]``：``.tmp-real-eval/real_queries.json``（真实 match_run 查询）；
- ``{JD: {vague|standard|colloquial: 查询}}``：``.tmp-judge/queries.json``（分层问法集）。

产出（``--output``，默认 ``.tmp-plan/parse_quality.json``）：
- 每条查询的解析产物摘要（来源 / 回退 / 采纳字段 / 解析耗时）与检索摘要（条数 / 降级 / 耗时）；
- 三类确定性违规明细（原文依据 / 城市词典 / 方向词表）与硬条件违反明细；
- 汇总：回退率、字段覆盖、解析与检索延迟分位、检索指标（有标签时含 R@20 / nDCG@10 对照）；
- ``gates``：文档 §评测与门槛 里解析相关门槛的逐条判定。

安全约定：不打印密钥；候选人 ID 一律 sha256 别名；不调用 ``optimize_pending()``（会写索引）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

from kerui_recruit.bench.parse_quality import (  # noqa: E402
    CANONICAL_FIELDS,
    CandidateAttrs,
    Violation,
    audit_candidate,
    audit_conditions,
    condition_selectivity,
    condition_values,
    summarize_conditions,
    summarize_plans,
    summarize_violations,
)
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.evaluation.retrieval import ndcg_at_k, recall_at_k  # noqa: E402
from kerui_recruit.providers.ai.catalog import CatalogService  # noqa: E402
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker  # noqa: E402
from kerui_recruit.providers.ai.config_store import AiConfigStore  # noqa: E402
from kerui_recruit.providers.ai.contracts import (  # noqa: E402
    ExecutionContext,
    ModelRole,
    TaskKind,
)
from kerui_recruit.providers.ai.manager import AiProviderManager  # noqa: E402
from kerui_recruit.providers.ai.probes import AiProbeService  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.parse import QueryParser  # noqa: E402
from kerui_recruit.search.query import parse_query  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
DEFAULT_OUTPUT = ROOT / ".tmp-plan" / "parse_quality.json"
DEFAULT_LABELS = ROOT / ".tmp-real-eval" / "real_labels.json"
# 硬条件复核需要的索引列：与 documents.build_candidate_document 的产出一致。
ATTR_COLUMNS = (
    "candidate_id", "chunk_type", "total_years", "highest_degree", "age", "qs_rank",
    "school_level", "school_tags", "school_region", "location", "location_terms",
    "preferred_location", "preferred_locations", "name_terms", "company_terms",
    "title_terms", "school_terms", "name_text", "company_text", "title_text",
    "school_text", "career_directions", "career_specializations", "business_directions",
    "direction", "keyword_index_text",
)


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_queries(path: Path, phrasings: tuple[str, ...]) -> list[tuple[str, str]]:
    """两种输入形状都归一成 ``[(qid, text), ...]``。"""
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


def load_attrs(root: Path) -> dict[str, list[CandidateAttrs]]:
    """从索引父行读取硬条件复核所需的候选人属性（只读）。

    同一候选人可能有多个当前修订 → 多条父行，因此按候选人群组：索引侧只要任一行过
    ``_where`` 该候选人就会被返回，复核也必须按「任一行满足即算满足」。
    """
    import lancedb

    table = lancedb.connect(str(root)).open_table("candidate_chunks")
    try:
        arrow = table.to_lance().to_table(columns=list(ATTR_COLUMNS))
    except Exception:
        arrow = table.to_arrow()
        arrow = arrow.drop([c for c in arrow.column_names if c not in ATTR_COLUMNS])
    data = {name: arrow.column(name).to_pylist() for name in arrow.column_names}
    attrs: dict[str, list[CandidateAttrs]] = {}
    for i in range(arrow.num_rows):
        if data.get("chunk_type", [None] * arrow.num_rows)[i] != "parent":
            continue
        row = {name: values[i] for name, values in data.items()}
        attrs.setdefault(str(data["candidate_id"][i]), []).append(CandidateAttrs.from_index_row(row))
    return attrs


def _ranked(ids: list[str], limit: int) -> list[str]:
    return ids[:limit]


async def run_search(service: HybridSearchService, keywords: str, filters, *, mode: str,
                     limit: int, semantic_query: str | None, concepts=()) -> dict:
    started = time.monotonic()
    page = await service.search(keywords, filters, limit=limit, mode=mode,
                                concepts=concepts, rewrite_enabled=False,
                                semantic_query=semantic_query)
    elapsed_ms = (time.monotonic() - started) * 1000
    ids: list[str] = []
    seen: set[str] = set()
    for hit in page.items:
        if hit.candidate_id not in seen:
            seen.add(hit.candidate_id)
            ids.append(hit.candidate_id)
    return {
        "ids": ids,
        "elapsed_ms": round(elapsed_ms, 1),
        "degraded": list(page.degraded_reasons),
        "empty_reason": page.empty_reason,
    }


def filters_from_conditions(values: dict[str, str]) -> CandidateFilters:
    """把报告里记录的「字段 -> 回显值」还原成 ``CandidateFilters``（离线重算用）。

    回显值带单位（``5年`` / ``QS前100``），数值字段需要抽数字；``degree_exact`` 这样的
    标志位不还原（保持默认 False，只会让选择性上界更松，不会误报「结构性无解」）。
    """
    numeric_float = ("min_years", "max_years")
    numeric_int = ("min_age", "max_age", "max_qs_rank")
    singles = ("highest_degree", "school_level", "school_region", "name", "company", "title",
               "school", "gender", "phone")
    kwargs: dict[str, object] = {}
    for field, raw in values.items():
        items = condition_values(field, raw)
        if not items:
            continue
        if field in numeric_float or field in numeric_int:
            match = re.search(r"\d+(?:\.\d+)?", items[0])
            if match is None:
                continue
            kwargs[field] = (float(match.group()) if field in numeric_float
                             else int(float(match.group())))
        elif field in singles:
            kwargs[field] = items[0]
        else:
            # location / preferred_location 等单值别名统一成多值字段。
            canonical = CANONICAL_FIELDS.get(field, field)
            kwargs[canonical] = tuple(items)
    return CandidateFilters(**kwargs)


async def main() -> None:
    parser_args = argparse.ArgumentParser()
    parser_args.add_argument("--queries", default=str(ROOT / ".tmp-real-eval" / "real_queries.json"))
    parser_args.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser_args.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser_args.add_argument("--mode", default="hybrid")
    parser_args.add_argument("--limit", type=int, default=50)
    parser_args.add_argument("--k", type=int, default=20, help="R@K 与 NDCG@10 的召回窗口")
    parser_args.add_argument("--max", type=int, default=0, help="只跑前 N 条（0 表示全部）")
    parser_args.add_argument("--only", default="", help="只跑这些 qid（逗号分隔），用于定点复跑")
    parser_args.add_argument("--phrasings", default="vague,standard,colloquial")
    parser_args.add_argument("--search-timeout", type=float, default=8.0)
    parser_args.add_argument("--dry-run", action="store_true", help="只验证接线与输入，不调模型")
    parser_args.add_argument("--recount", default="",
                             help="离线重算已跑报告的条件选择性（不调模型、不检索）")
    args = parser_args.parse_args()

    queries_path = Path(args.queries)
    if not queries_path.exists():
        raise SystemExit(f"缺少查询集 {queries_path}")
    queries = load_queries(queries_path, tuple(p.strip() for p in args.phrasings.split(",") if p.strip()))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        queries = [item for item in queries if item[0] in wanted]
    if args.max:
        queries = queries[:args.max]
    print(f"查询集={queries_path.name} 条数={len(queries)} 模式={args.mode} limit={args.limit}")

    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model=settings["siliconflow_embedding_model"])
    print(f"index is_ready={index.is_ready()} is_compatible={index.is_compatible()}")
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")
    attrs = load_attrs(DEV / "search")
    print(f"父行候选人属性={len(attrs)}")

    # 离线重算模式：拿已跑报告里的条件值重建 filters，只重新算选择性，全程不调模型、不检索。
    if args.recount:
        report_path = Path(args.recount)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        recounted: list[dict] = []
        for record in report.get("records", []):
            values = record.get("condition_values") or {}
            if not values:
                continue
            filters = filters_from_conditions(values)
            selectivity = condition_selectivity(filters, attrs)
            record["selectivity"] = selectivity
            if record.get("search_on", {}).get("n") == 0:
                recounted.append({
                    "qid": record["qid"], "satisfying_all": selectivity["satisfying_all"],
                    "per_field": selectivity["per_field"], "conditions": values,
                })
        report["zero_result_attribution"] = recounted
        report["recounted"] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"离线重算完成：空结果查询={len(recounted)} 报告已更新 {report_path}")
        for item in recounted:
            print(f"  {item['qid']} 条件可过(上界)={item['satisfying_all']} 单条={item['per_field']}")
        return

    async with httpx.AsyncClient(timeout=60) as client:
        encryption = EncryptionService(str(DEV / "config" / "encryption.key"))
        catalog = CatalogService(cache_path=DEV / "config" / "ai-catalog.json")
        config_store = AiConfigStore(
            path=DEV / "config" / "ai-providers.json",
            encryption=encryption,
            catalog_service=catalog,
            legacy_settings_path=DEV / "config" / "settings.json",
        )
        manager = AiProviderManager(
            config_store=config_store,
            catalog_service=catalog,
            probe_service=AiProbeService(catalog, client),
            http_client=client,
            circuit_breaker=CircuitBreaker(),
        )
        # 与 runtime.py 同一装配口径：解析走 FAST_TEXT + INTERACTIVE。
        parser = QueryParser(
            manager.task_client(TaskKind.QUERY_PARSE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
        )
        embedding = SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_reranker_model"])
        service = HybridSearchService(
            index=index, embedding_provider=embedding, reranker_provider=reranker,
            search_timeout=args.search_timeout)
        print(f"models embedding={embedding.model} reranker={reranker.model}")

        if args.dry_run:
            print("dry-run：接线与输入校验通过；未调用解析模型与检索（正式抽检去掉 --dry-run）。")
            print("示例查询：", queries[0] if queries else "-")
            await manager.close()
            return

        labels: dict[str, dict[str, int]] = {}
        labels_path = Path(args.labels)
        if labels_path.exists():
            labels = json.loads(labels_path.read_text(encoding="utf-8"))

        records: list[dict[str, Any]] = []
        violations: list[Violation] = []
        per_query_violations: dict[str, int] = {}
        hard_filter_violations = 0
        checked_candidates = 0
        ranking_on: dict[str, list[str]] = {}
        ranking_off: dict[str, list[str]] = {}
        # 开启解析后为空的查询及其条件选择性归因（离线算，不发请求）。
        zero_result: list[dict[str, Any]] = []

        for qid, text in queries:
            parse_started = time.monotonic()
            plan = await parser.parse(text)
            parse_ms = (time.monotonic() - parse_started) * 1000
            rule = parse_query(text)

            on = await run_search(service, plan.keywords, plan.filters, mode=args.mode,
                                  limit=args.limit, semantic_query=plan.semantic_query,
                                  concepts=plan.concepts)
            off = await run_search(service, rule.keywords, rule.filters, mode=args.mode,
                                   limit=args.limit, semantic_query=None, concepts=rule.concepts)
            ranking_on[qid] = [alias(cid) for cid in _ranked(on["ids"], args.k)]
            ranking_off[qid] = [alias(cid) for cid in _ranked(off["ids"], args.k)]

            conditions = [(c.field, c.value) for c in plan.conditions]
            query_violations = audit_conditions(text, conditions)
            for cid in on["ids"]:
                rows = attrs.get(cid)
                if not rows:
                    continue
                checked_candidates += 1
                query_violations.extend(audit_candidate(plan.filters, rows))
            violations.extend(query_violations)
            per_query_violations[qid] = len(query_violations)
            hard_filter_violations += sum(1 for item in query_violations if item.kind == "hard_filter")

            # 离线算条件选择性：解释「开解析后结果为空」是哪条条件造成的（不发请求）。
            selectivity = condition_selectivity(plan.filters, attrs)
            if not on["ids"]:
                zero_result.append({"qid": qid, "satisfying_all": selectivity["satisfying_all"],
                                    "per_field": selectivity["per_field"],
                                    "conditions": {c.field: c.value for c in plan.conditions}})

            records.append({
                "qid": qid,
                "text_sha": alias(text),
                "text_len": len(text),
                "source": plan.source,
                "degraded": plan.degraded,
                "accepted_fields": list(plan.accepted_fields),
                "elapsed_ms": round(parse_ms, 1),
                "conditions": {c.field: c.confidence for c in plan.conditions},
                "condition_values": {c.field: c.value for c in plan.conditions},
                "keyword_terms_len": len(plan.keywords),
                "semantic_query": bool(plan.semantic_query),
                "unparsed": list(plan.unparsed_terms),
                "selectivity": selectivity,
                "search_on": {"n": len(on["ids"]), "ms": on["elapsed_ms"], "degraded": on["degraded"]},
                "search_off": {"n": len(off["ids"]), "ms": off["elapsed_ms"], "degraded": off["degraded"]},
            })
            print(f"  {qid} src={plan.source} degraded={plan.degraded or '-'} "
                  f"conds={len(conditions)} parse={parse_ms:.0f}ms "
                  f"on={len(on['ids'])} off={len(off['ids'])} 违规={len(query_violations)} "
                  f"条件可过(上界)={selectivity['satisfying_all']}", flush=True)
            # 增量落盘：长跑中途崩了也不至于把已跑出的证据全丢掉。
            if len(records) % 10 == 0:
                _write_report(args.output, partial=True, input_info={
                    "queries": str(queries_path), "count": len(queries), "done": len(records),
                    "mode": args.mode, "limit": args.limit, "k": args.k,
                    "index_ready": index.is_ready(), "index_compatible": index.is_compatible(),
                    "candidate_attrs": len(attrs), "labels": str(labels_path) if labels else None,
                }, records=records, violations=violations,
                    per_query_violations=per_query_violations, zero_result=zero_result,
                    plan_summary=summarize_plans(records),
                    retrieval={"parse_on": None, "parse_off": None}, gates=None)

        labeled = [(qid, labels[qid]) for qid, _ in queries if qid in labels and labels[qid]]
        metrics_on = _retrieval_metrics([qid for qid, _ in labeled], ranking_on, labels)
        metrics_off = _retrieval_metrics([qid for qid, _ in labeled], ranking_off, labels)
        plan_summary = summarize_plans(records)
        violation_summary = summarize_violations(violations)

        # 门槛判定（文档 §评测与门槛「解析」与「默认开启解析」两组）。
        span_kinds = ("span_missing", "phone_not_verbatim")
        risky = ("name", "gender", "phone")
        risky_violations = [v for v in violations if v.kind in span_kinds and v.field in risky]
        gates = {
            "hard_filter_violations_zero": hard_filter_violations == 0,
            "hard_filter_violations": hard_filter_violations,
            "candidates_checked": checked_candidates,
            "risky_field_misjudgement_zero": not risky_violations,
            "risky_field_misjudgements": [
                {"field": v.field, "value": v.value, "kind": v.kind} for v in risky_violations],
            "direction_misjudgement_zero": "direction_unverified" not in violation_summary,
            "direction_misjudgements": violation_summary.get("direction_unverified", 0),
            "city_misjudgement_zero": "city_unknown" not in violation_summary,
            "city_misjudgements": violation_summary.get("city_unknown", 0),
            "fallback_complete_rate": round(1 - (plan_summary["degraded_rate"] or 0), 4),
            "zero_result_queries": len(zero_result),
            "zero_result_by_construction": sum(
                1 for item in zero_result if item["satisfying_all"] == 0),
            "ndcg10_not_worse": (
                None if metrics_on["ndcg10"] is None or metrics_off["ndcg10"] is None
                else metrics_on["ndcg10"] >= metrics_off["ndcg10"]),
            "recall20_improved": (
                None if metrics_on["recall20"] is None or metrics_off["recall20"] is None
                else metrics_on["recall20"] > metrics_off["recall20"]),
            "labeled_queries": len(labeled),
        }

        REPORT_INPUT = {
            "queries": str(queries_path), "count": len(queries), "mode": args.mode,
            "limit": args.limit, "k": args.k,
            "index_ready": index.is_ready(), "index_compatible": index.is_compatible(),
            "candidate_attrs": len(attrs), "labels": str(labels_path) if labels else None,
        }
        output = _write_report(
            args.output, partial=False, input_info=REPORT_INPUT, records=records,
            violations=violations, per_query_violations=per_query_violations,
            zero_result=zero_result, plan_summary=plan_summary,
            retrieval={"parse_on": metrics_on, "parse_off": metrics_off}, gates=gates)

        print("\n=== 解析质量抽检 ===")
        print(f"回退率={plan_summary['degraded_rate']} 回退类型={plan_summary['degraded_kinds']}")
        print(f"来源分布={plan_summary['source_distribution']}")
        print(f"解析延迟 p50/p95/max={plan_summary['parse_ms_p50']}/"
              f"{plan_summary['parse_ms_p95']}/{plan_summary['parse_ms_max']} ms")
        print(f"违规分布={violation_summary or '无'}")
        print(f"硬条件违反={hard_filter_violations}（复核候选人 {checked_candidates}）")
        print(f"开解析后空结果={len(zero_result)} 条，其中条件组合结构性无解="
              f"{sum(1 for item in zero_result if item['satisfying_all'] == 0)} 条")
        for item in zero_result[:5]:
            print(f"    空结果归因 {item['qid']} 条件可过(上界)={item['satisfying_all']} "
                  f"单条选择性={item['per_field']} 条件={item['conditions']}")
        if labeled:
            print(f"有标签查询={len(labeled)} R@{args.k} on/off={metrics_on['recall20']}/"
                  f"{metrics_off['recall20']} nDCG@10 on/off={metrics_on['ndcg10']}/{metrics_off['ndcg10']}")
        print(f"门槛判定={json.dumps(gates, ensure_ascii=False)}")
        print(f"报告已写入 {output}")
        await manager.close()


def _write_report(path: str, *, partial: bool, input_info: dict, records: list,
                  violations: list, per_query_violations: dict, zero_result: list,
                  plan_summary: dict, retrieval: dict, gates: dict | None) -> Path:
    """写报告；``partial=True`` 是跑到一半的中间产物（阈值判定为 None）。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "partial": partial,
        "input": input_info,
        "plans": plan_summary,
        "conditions": summarize_conditions(records),
        "violations": summarize_violations(violations),
        "per_query_violations": {k: v for k, v in per_query_violations.items() if v},
        "violation_details": [
            {"kind": v.kind, "field": v.field, "value": v.value, "detail": v.detail}
            for v in violations[:200]
        ],
        "retrieval": retrieval,
        "gates": gates,
        "zero_result_attribution": zero_result,
        "records": records,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _retrieval_metrics(qids: list[str], ranking: dict[str, list[str]],
                       labels: dict[str, dict[str, int]]) -> dict:
    """检索指标：`ndcg_at_k` 在无相关项/空排序时会返回 None，均值前必须过滤。"""
    if not qids:
        return {"n": 0, "recall20": None, "ndcg10": None}
    recalls, ndcgs = [], []
    for qid in qids:
        relevant = {cid: int(grade) for cid, grade in (labels.get(qid) or {}).items()}
        ranked = ranking.get(qid, [])
        recall = recall_at_k(ranked, relevant, 20)
        ndcg = ndcg_at_k(ranked, relevant, 10)
        if recall is not None:
            recalls.append(recall)
        if ndcg is not None:
            ndcgs.append(ndcg)
    return {
        "n": len(qids),
        "recall20": round(statistics.mean(recalls), 4) if recalls else None,
        "ndcg10": round(statistics.mean(ndcgs), 4) if ndcgs else None,
        "recall20_n": len(recalls),
        "ndcg10_n": len(ndcgs),
    }


if __name__ == "__main__":
    asyncio.run(main())
