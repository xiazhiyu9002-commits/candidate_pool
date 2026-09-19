"""验证 ``search_fts_boolean``（keyword 模式的 and/or 分支）是否让 child chunk 进入候选池。

静态事实（已逐行核对）：
- ``search_fts``（operator=smart 分支）在 ``search_body=False`` 时追加
  ``chunk_type = 'parent'``（lancedb_index.py:367-370），把子 chunk 隔离在池外；
- ``search_fts_boolean``（operator=and/or 分支，lancedb_index.py:383-425）既无 chunk_type
  过滤、也不接收 ``search_body``，只检索 ``keyword_index_text``；
- service._keyword_retrieve:227-233 让 and/or 走该分支，且该分支不做
  ``_filter_by_hit_terms``（smart 分支做）。

本脚本在真实只读索引（``.dev-data/search``）上做单变量隔离实验：
  ``{and,or}_current`` = 生产行为；``{and,or}_parent`` = ``where`` 追加
  ``chunk_type='parent'``（其余逻辑逐字一致）。
对照 smart 路径（search_body=False / True）。
量化：代表行 child 占比、候选人净增、与 parent-only 池的差集、99 条弱监督标签上的排序指标差。

关于速度：生产 ``search_fts_boolean`` 用「窗口倍增重试」扩大召回，``and`` 模式下命中稀少
时会一路翻倍到全表，且每次 ``to_list()`` 都带 1024 维向量列 → 单查询可达数十秒。本脚本用
``_boolean_rows`` 复刻同一条匹配/去重语义，但一次性 ``select`` 精简列后单遍扫描，等价性由
``validate_boolean`` 用生产函数（带 deadline）抽查证明。

安全：只读；**不调用** ``optimize_pending()``（``.fts-dirty`` 保留）；不打印密钥；产物全为别名。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.evaluation.retrieval import (  # noqa: E402
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import (  # noqa: E402
    SEARCH_DEADLINE,
    LanceDBSearchIndex,
    _concept_token_sets,
    _matches_concepts,
)
from kerui_recruit.search.query import parse_query  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL_DIR = ROOT / ".tmp-real-eval"
# service: pool_limit = min(max(limit * 3, 100), 5000)；真实集 limit=100 → 300
POOL, CAP = 300, 100
# 精简列：够 ``hits_from_rows``/``_hit_from_row`` 构 SearchHit（id/revision_id 必填），
# 且不含 1024 维向量列。
SCOPE_COLUMNS = ["id", "candidate_id", "revision_id", "chunk_type", "keyword_index_text"]


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class _ParentOnlyChunkIndex(LanceDBSearchIndex):
    """任意通道都限定 ``chunk_type='parent'``：只覆盖 ``_where``，检索逻辑与生产逐字一致。"""

    def _where(self, filters: CandidateFilters) -> str:
        base = super()._where(filters)
        return " AND ".join(c for c in (base, "chunk_type = 'parent'") if c)


class _ObservingIndex(LanceDBSearchIndex):
    """记录传给 ``hits_from_rows`` 的行数，用于证明分支确实被走到。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.captured: list[tuple[str, int]] = []

    def hits_from_rows(self, rows, channel):
        self.captured.append((channel, len(rows)))
        return super().hits_from_rows(rows, channel)


def ranked(index: LanceDBSearchIndex, rows: list[dict]) -> list[str]:
    """按生产 keyword 路径把原始行转成别名排序（hits_from_rows → 候选人去重 → cap）。"""
    seen: set[str] = set()
    out: list[str] = []
    for hit in index.hits_from_rows(rows, "bm25"):
        key = alias(hit.candidate_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= CAP:
            break
    return out


def chunk_stats(rows: list[dict]) -> dict:
    """``search_fts`` / ``search_fts_boolean`` 返回前均已按候选人去重 → 行即代表行。"""
    child = sum(1 for r in rows if r.get("chunk_type") == "child")
    parent = sum(1 for r in rows if r.get("chunk_type") == "parent")
    return {"rows": len(rows), "child": child, "parent": parent,
            "other": len(rows) - child - parent}


def bounded(seconds: float, function, *args):
    """按生产同款 deadline 调用索引原语；超时/异常返回 (None, 原因)，不抛出。"""
    token = SEARCH_DEADLINE.set(time.monotonic() + seconds)
    try:
        return function(*args), None
    except TimeoutError:
        return None, "TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - 只用于把生产异常显式暴露出来
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        SEARCH_DEADLINE.reset(token)


def _boolean_rows(table, concepts, operator: str, where: str, limit: int, row_count: int) -> list[dict]:
    """复刻 ``search_fts_boolean``（lancedb_index.py:399-425）的匹配/去重语义，单遍扫列。

    与生产唯一的差别是 ``select`` 精简列（不取 1024 维向量，避免窗口倍增时反复物化全表）。
    生产按 FTS 得分序处理行、候选人首次出现即定序，故取前 ``limit`` 个匹配与生产一致。
    """
    fts_query = " ".join(dict.fromkeys(
        alias_.casefold() for concept in concepts for alias_ in concept.aliases))
    if not fts_query:
        return []
    builder = table.search(fts_query, query_type="fts", fts_columns="keyword_index_text")
    if where:
        builder = builder.where(where)
    rows = builder.select(SCOPE_COLUMNS).limit(row_count).to_list()
    token_sets = _concept_token_sets(concepts)
    matched: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        candidate_id = row["candidate_id"]
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        if _matches_concepts(row.get("keyword_index_text") or "", token_sets, operator):
            matched.append(row)
            if len(matched) >= limit:
                break
    return matched


def validate_boolean(index: LanceDBSearchIndex, samples: list[tuple[str, str, tuple]],
                     seconds: float) -> None:
    """抽查生产 ``search_fts_boolean`` 与 ``_boolean_rows`` 的结果是否一致。"""
    table = index.database.open_table(index.table_name)
    row_count = table.count_rows()
    filters = CandidateFilters()
    print("\n生产函数等价性抽查（alias 排序逐位比较；生产带 deadline）:")
    for qid, keywords, concepts in samples:
        for operator in ("or", "and"):
            started = time.monotonic()
            production, error = bounded(seconds, index.search_fts_boolean, keywords,
                                        concepts, operator, filters, POOL)
            elapsed = time.monotonic() - started
            if production is None:
                print(f"  {qid} op={operator:<3} 生产={error}（{elapsed:.1f}s）→ 跳过比对")
                continue
            fast = _boolean_rows(table, concepts, operator, index._where(filters), POOL, row_count)
            same = ranked(index, production) == ranked(index, fast)
            print(f"  {qid} op={operator:<3} 生产行={len(production)} 复刻行={len(fast)} "
                  f"一致={same}（生产耗时 {elapsed:.1f}s）")


def _mean(values):
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def metrics(qids: list[str], ranked_map: dict[str, list[str]],
            cases: dict[str, dict]) -> dict:
    golden = lambda q, t: {c for c, g in cases[q]["relevant"].items() if g >= t}  # noqa: E731
    cap = _mean([min(1.0, 20 / len(golden(q, 1))) for q in qids if golden(q, 1)])
    recall20 = _mean([recall_at_k(ranked_map[q], golden(q, 1), 20) for q in qids])
    return {
        "n": len(qids),
        "recall20": recall20,
        "cap20": cap,
        "norm20": round(recall20 / cap, 4) if recall20 is not None and cap else None,
        "recall20_g2": _mean([recall_at_k(ranked_map[q], golden(q, 2), 20) for q in qids]),
        "recall20_g3": _mean([recall_at_k(ranked_map[q], golden(q, 3), 20) for q in qids]),
        "ndcg10": _mean([ndcg_at_k(ranked_map[q], cases[q]["relevant"], 10) for q in qids]),
        "mrr20": _mean([mrr_at_k(ranked_map[q], cases[q]["relevant"], 20) for q in qids]),
        "p5_g2": _mean([
            (sum(1 for c in ranked_map[q][:5] if cases[q]["relevant"].get(c, 0) >= 2) / 5)
            if ranked_map[q] else None for q in qids]),
        "p10_g3": _mean([
            (sum(1 for c in ranked_map[q][:10] if cases[q]["relevant"].get(c, 0) >= 3) / 10)
            if ranked_map[q] else None for q in qids]),
        "empty": sum(1 for q in qids if not ranked_map[q]),
    }


async def service_probe(model: str, samples: list[tuple[str, str, tuple]]) -> None:
    """端到端确认：分支被走到、``search_body`` 在 and/or 下是否无效。"""

    class _Unused:
        async def embed_query(self, query):  # pragma: no cover - keyword 模式不调用
            raise RuntimeError("provider not used in keyword mode")

        async def rerank_scored(self, query, contents):  # pragma: no cover
            raise RuntimeError("provider not used in keyword mode")

    index = _ObservingIndex(DEV / "search", vector_dimension=1024, embedding_model=model)
    service = HybridSearchService(index=index, embedding_provider=_Unused(),
                                 reranker_provider=_Unused(), search_timeout=30.0)
    print("\n端到端（service.search, mode=keyword）:")
    print(f"  ObservingIndex is_ready={index.is_ready()}")
    for qid, keywords, concepts in samples:
        for operator in ("smart", "and", "or"):
            if operator in ("and", "or") and not concepts:
                continue
            for search_body in (False, True):
                index.captured.clear()
                page = await service.search(keywords, CandidateFilters(), limit=CAP,
                                            mode="keyword", operator=operator,
                                            concepts=concepts, search_body=search_body)
                print(f"  {qid} op={operator:<5} body={int(search_body)} items={len(page.items):>3} "
                      f"empty={page.empty_reason or '-'} degraded={list(page.degraded_reasons) or '-'} "
                      f"captured={index.captured}", flush=True)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    model = settings["siliconflow_embedding_model"]
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024, embedding_model=model)
    print(f"index is_ready={index.is_ready()} is_compatible={index.is_compatible()} "
          f"read_error={index.read_compatibility_error}")
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    table = index.database.open_table(index.table_name)
    row_count = table.count_rows()
    parent_candidates: set[str] = set()
    try:
        rows = (table.search(None).where("chunk_type = 'parent'")
                .select(["candidate_id"]).limit(row_count).to_list())
        parent_candidates = {r["candidate_id"] for r in rows}
    except Exception as exc:  # noqa: BLE001 - 该子指标缺失不影响主结论
        print(f"（parent 候选人映射不可用：{type(exc).__name__}: {exc}）")
    print(f"索引 rows={row_count} parent 候选人数={len(parent_candidates) or '未知'}")

    queries = json.loads((EVAL_DIR / "real_queries.json").read_text(encoding="utf-8"))
    labels = json.loads((EVAL_DIR / "real_labels.json").read_text(encoding="utf-8"))
    labelled = [q for q in queries if q["qid"] in labels]
    cases = {q["qid"]: {"id": q["qid"],
                        "relevant": {c: int(g) for c, g in labels[q["qid"]].items()}}
             for q in labelled}
    print(f"查询总数={len(queries)} 有标签={len(labelled)}")

    parsed = {q["qid"]: parse_query(q["text"]) for q in labelled}
    concept_qids = [q["qid"] for q in labelled if parsed[q["qid"]].concepts]
    print(f"有 concepts（and/or 分支真实可达）的查询={len(concept_qids)}/{len(labelled)}")

    ranked_map: dict[str, dict[str, list[str]]] = {}
    cand_sets: dict[str, dict[str, set[str]]] = {}
    stats_map: dict[str, dict[str, dict]] = {}
    filters = CandidateFilters()
    where_all = index._where(filters)
    where_parent = _ParentOnlyChunkIndex(DEV / "search", vector_dimension=1024,
                                        embedding_model=model)._where(filters)

    def record(name: str, qid: str, rows: list[dict]) -> None:
        ranked_map.setdefault(name, {})[qid] = ranked(index, rows)
        cand_sets.setdefault(name, {})[qid] = {r["candidate_id"] for r in rows}
        stats_map.setdefault(name, {})[qid] = chunk_stats(rows)

    for query in labelled:
        qid = query["qid"]
        keywords, concepts = parsed[qid].keywords, parsed[qid].concepts
        smart = HybridSearchService._filter_by_hit_terms(
            list(index.search_fts(keywords, filters, POOL, False)), keywords)
        smart_body = HybridSearchService._filter_by_hit_terms(
            list(index.search_fts(keywords, filters, POOL, True)), keywords)
        record("smart", qid, smart)
        record("smart_body", qid, smart_body)
        for operator in ("and", "or"):
            if concepts:
                record(f"{operator}_current", qid, _boolean_rows(
                    table, concepts, operator, where_all, POOL, row_count))
                record(f"{operator}_parent", qid, _boolean_rows(
                    table, concepts, operator, where_parent, POOL, row_count))
            else:
                # 生产同款回退：无 concepts 时 and/or 走 smart 分支
                record(f"{operator}_current", qid, smart)
                record(f"{operator}_parent", qid, smart)
        print(f"  {qid} concepts={len(concepts)} smart={len(smart)} "
              f"and/cur={len(ranked_map['and_current'][qid])} "
              f"or/cur={len(ranked_map['or_current'][qid])}", flush=True)

    ref_path = EVAL_DIR / "retrieval" / "real_keyword.json"
    if ref_path.exists():
        reference = json.loads(ref_path.read_text(encoding="utf-8"))
        mismatch = sum(1 for q in labelled
                       if ranked_map["smart"][q["qid"]]
                       != list((reference.get(q["qid"]) or {}).get("keyword") or []))
        print(f"\nsmart 复现 vs real_keyword.json：不一致查询={mismatch}/{len(labelled)}")

    print("\n代表行构成（限有 concepts 的 %d 条；行=候选人，返回前已去重）：" % len(concept_qids))
    header = f"{'variant':<15}{'rows':>8}{'child':>8}{'child占比':>11}{'parent':>8}{'other':>8}"
    print(header)
    print("-" * len(header))
    for name in ("smart", "smart_body", "and_current", "and_parent", "or_current", "or_parent"):
        rows = [stats_map[name][q] for q in concept_qids]
        total_rows = sum(s["rows"] for s in rows)
        child = sum(s["child"] for s in rows)
        parent = sum(s["parent"] for s in rows)
        other = sum(s["other"] for s in rows)
        share = round(child / total_rows, 4) if total_rows else None
        print(f"{name:<15}{total_rows:>8}{child:>8}{str(share):>11}{parent:>8}{other:>8}")

    print("\n候选池效应（相对 parent-only 对照）：")
    for operator in ("and", "or"):
        cur, par = f"{operator}_current", f"{operator}_parent"
        only_child = sum(len(cand_sets[cur][q] - cand_sets[par][q]) for q in concept_qids)
        lost = sum(len(cand_sets[par][q] - cand_sets[cur][q]) for q in concept_qids)
        jaccard = _mean([len(cand_sets[cur][q] & cand_sets[par][q])
                         / max(1, len(cand_sets[cur][q] | cand_sets[par][q]))
                         for q in concept_qids])
        queries_with_child = sum(1 for q in concept_qids if stats_map[cur][q]["child"])
        child_only: set[str] = set()
        for q in concept_qids:
            child_only |= (cand_sets[cur][q] - cand_sets[par][q])
        have_parent = len(child_only & parent_candidates) if parent_candidates else None
        print(f"  {cur}: 仅靠 child 进入池的候选人数={only_child} "
              f"（其中索引里存在父 chunk 的={have_parent}） 对照独有={lost} "
              f"Jaccard={jaccard} 含 child 代表行的查询数={queries_with_child}/{len(concept_qids)}")

    print("\n排序指标（限有 concepts 的查询子集）:")
    keys = ("recall20", "norm20", "recall20_g2", "recall20_g3", "ndcg10", "mrr20",
            "p5_g2", "p10_g3", "empty")
    width = 12
    head = f"{'variant':<15}{'n':>5}" + "".join(f"{k:>{width}}" for k in keys)
    print(head)
    print("-" * len(head))
    for name in ("smart", "smart_body", "and_current", "and_parent", "or_current", "or_parent"):
        row = metrics(concept_qids, ranked_map[name], cases)
        print(f"{name:<15}{row['n']:>5}" + "".join(
            f"{'n/a':>{width}}" if row.get(k) is None else f"{row[k]:>{width}}" for k in keys))

    print("\n附带：smart 路径在全部 99 条上的复现值（对照真实评测表）:")
    for name in ("smart", "smart_body"):
        row = metrics([q["qid"] for q in labelled], ranked_map[name], cases)
        print(f"  {name:<10} recall20={row['recall20']} norm={row['norm20']} "
              f"ndcg10={row['ndcg10']} mrr20={row['mrr20']} empty={row['empty']}")

    samples = [(q, parsed[q].keywords, parsed[q].concepts) for q in concept_qids[:3]]
    if samples:
        validate_boolean(index, samples, seconds=60.0)
        asyncio.run(service_probe(model, samples))


if __name__ == "__main__":
    main()
