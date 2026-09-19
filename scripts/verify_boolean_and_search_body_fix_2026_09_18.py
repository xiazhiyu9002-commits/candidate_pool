"""验证「登记的两个缺陷」的修复（第十五轮）：and 恒空 + ``search_body`` 在 and/or 下是死开关。

被验证的修复（backend/src/kerui_recruit/search）：
- ``search_fts_boolean`` 新增 ``search_body`` 参数，``search_body=False`` 时追加
  ``chunk_type = 'parent'``（与 ``search_fts`` 对齐）→ 缺陷②「死开关」消除；
- ``and`` 改为**候选人级**求与，且只对词表已策展 concept（``is_curated_concept``）求与，
  无策展 concept 时退化为 or → 缺陷①「99/99 恒空」消除。

本脚本在真实只读索引（``.dev-data/search``）上做三件事：
A. 全量查询扫描：用与生产逐字等价、但 ``select`` 精简列的单遍复刻，比较
   ``and_legacy``（旧：单 chunk AND + 全部 concept）与 ``and_fixed``（新：候选人级 + 仅策展）
   的 ``empty`` 计数与候选人数 → 量化缺陷①修复幅度；
   并比较 ``or_fixed_parent``（body=False）与 ``or_fixed_all``（body=True）→ 量化缺陷②的影响面。
B. 生产函数抽查（带 deadline）：直接调用生产 ``search_fts_boolean``，与复刻逐位比对别名排序，
   并对照 ``search_body ∈ {False, True}`` 的结果差异 → 证明修复已落到生产路径。
C. 端到端：``service.search(mode='keyword')`` 在 and/or × body 上的输出与耗时。

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

from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import (  # noqa: E402
    SEARCH_DEADLINE,
    LanceDBSearchIndex,
    _concept_token_sets,
    _matched_concept_indices,
    _matches_concepts,
)
from kerui_recruit.search.lexicon import is_curated_concept  # noqa: E402
from kerui_recruit.search.query import parse_query  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL_DIR = ROOT / ".tmp-real-eval"
# service: pool_limit = min(max(limit * 3, 100), 5000)；真实集 limit=100 → 300
POOL, CAP = 300, 100
SCOPE_COLUMNS = ["id", "candidate_id", "revision_id", "chunk_type", "keyword_index_text"]
# 生产 and 走窗口倍增 + 全列物化，单查询可能很慢；抽查统一用该上限截断。
PROD_SECONDS = 20.0
SAMPLE_SIZE = 3


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


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


def _fts_rows(table, fts_query: str, where: str, row_count: int) -> list[dict]:
    builder = table.search(fts_query, query_type="fts", fts_columns="keyword_index_text")
    if where:
        builder = builder.where(where)
    return builder.select(SCOPE_COLUMNS).limit(row_count).to_list()


def _legacy_boolean_rows(table, concepts, operator: str, where: str,
                         limit: int, row_count: int) -> list[dict]:
    """复刻**修复前**的 ``search_fts_boolean``：单 chunk AND + 全部 concept、无 parent 过滤。"""
    fts_query = " ".join(dict.fromkeys(
        item.casefold() for concept in concepts for item in concept.aliases))
    if not fts_query:
        return []
    token_sets = _concept_token_sets(concepts)
    matched: list[dict] = []
    seen: set[str] = set()
    for row in _fts_rows(table, fts_query, where, row_count):
        candidate_id = row["candidate_id"]
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        if _matches_concepts(row.get("keyword_index_text") or "", token_sets, operator):
            matched.append(row)
            if len(matched) >= limit:
                break
    return matched


def _fixed_boolean_rows(table, concepts, operator: str, where: str,
                        limit: int, row_count: int) -> list[dict]:
    """复刻**修复后**的 ``search_fts_boolean``：and = 候选人级 + 仅策展 concept；含 body 过滤。"""
    required = concepts
    if operator == "and":
        curated = tuple(c for c in concepts if is_curated_concept(c.canonical))
        if curated:
            required = curated
        else:
            operator = "or"
    fts_query = " ".join(dict.fromkeys(
        item.casefold() for concept in required for item in concept.aliases))
    if not fts_query:
        return []
    token_sets = _concept_token_sets(required)
    rows = _fts_rows(table, fts_query, where, row_count)
    if operator == "and":
        representatives: dict[str, dict] = {}
        progress: dict[str, set[int]] = {}
        for row in rows:
            candidate_id = row["candidate_id"]
            representatives.setdefault(candidate_id, row)
            progress.setdefault(candidate_id, set()).update(
                _matched_concept_indices(row.get("keyword_index_text") or "", token_sets))
        complete = [cid for cid, hits in progress.items() if len(hits) == len(token_sets)]
        return [representatives[cid] for cid in complete[:limit]]
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


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    model = settings["siliconflow_embedding_model"]
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024, embedding_model=model)
    print(f"index is_ready={index.is_ready()} is_compatible={index.is_compatible()} "
          f"read_error={index.read_compatibility_error}")
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    table = index.database.open_table(index.table_name)
    row_count = table.count_rows()
    print(f"索引 rows={row_count}")

    queries = json.loads((EVAL_DIR / "real_queries.json").read_text(encoding="utf-8"))
    labels_path = EVAL_DIR / "real_labels.json"
    labelled = json.loads(labels_path.read_text(encoding="utf-8")).keys() if labels_path.exists() else ()
    parsed = {q["qid"]: parse_query(q["text"]) for q in queries}
    concept_queries = [q for q in queries if parsed[q["qid"]].concepts]
    print(f"查询总数={len(queries)} 有 concepts（and/or 分支真实可达）="
          f"{len(concept_queries)}/${len(queries)} 有弱监督标签={len(labelled)}")

    filters = CandidateFilters()
    where_all = index._where(filters)
    where_parent = " AND ".join(c for c in (where_all, "chunk_type = 'parent'") if c)

    # ---- A. 全量扫描：缺陷① 修复幅度 + 缺陷② 影响面 ----
    print("\nA. 全量扫描（与生产逐字等价、select 精简列的单遍复刻）:")
    header = (f"{'variant':<18}{'empty':>7}{'empty占比':>11}{'候选人数合计':>14}{'单查询均候选':>14}")
    print(header)
    print("-" * len(header))
    variants = {
        # 旧 and：单 chunk AND + 全部 concept（= 登记缺陷①的行为）
        "and_legacy": lambda q: _legacy_boolean_rows(
            table, parsed[q["qid"]].concepts, "and", where_all, POOL, row_count),
        # 新 and：候选人级 + 仅策展 concept，body=False（生产默认）→ parent-only
        "and_fixed_body0": lambda q: _fixed_boolean_rows(
            table, parsed[q["qid"]].concepts, "and", where_parent, POOL, row_count),
        # 新 and：body=True → 也检索 child
        "and_fixed_body1": lambda q: _fixed_boolean_rows(
            table, parsed[q["qid"]].concepts, "and", where_all, POOL, row_count),
        # 新 or：body=False / True —— 缺陷②的影响面（修复后两者应不同）
        "or_fixed_body0": lambda q: _fixed_boolean_rows(
            table, parsed[q["qid"]].concepts, "or", where_parent, POOL, row_count),
        "or_fixed_body1": lambda q: _fixed_boolean_rows(
            table, parsed[q["qid"]].concepts, "or", where_all, POOL, row_count),
    }
    report: dict[str, dict[str, list[str]]] = {}
    for name, run in variants.items():
        report[name] = {q["qid"]: ranked(index, run(q)) for q in concept_queries}
        empty = sum(1 for q in concept_queries if not report[name][q["qid"]])
        total = sum(len(report[name][q["qid"]]) for q in concept_queries)
        share = round(empty / len(concept_queries), 4) if concept_queries else None
        print(f"{name:<18}{empty:>7}{str(share):>11}{total:>14}"
              f"{round(total / len(concept_queries), 2):>14}", flush=True)

    body_changed = sum(1 for q in concept_queries
                       if report["or_fixed_body0"][q["qid"]] != report["or_fixed_body1"][q["qid"]])
    print(f"\n缺陷②（or）：body=False 与 body=True 结果不同的查询数="
          f"{body_changed}/{len(concept_queries)}；body=True 多出的候选总数="
          f"{sum(len(report['or_fixed_body1'][q['qid']]) - len(report['or_fixed_body0'][q['qid']]) for q in concept_queries)}")

    # ---- B. 生产函数抽查：等价性 + body 开关真实生效 ----
    samples = concept_queries[:SAMPLE_SIZE]
    print(f"\nB. 生产 search_fts_boolean 抽查（{len(samples)} 条 × and/or × body，"
          f"deadline={PROD_SECONDS:.0f}s）:")
    for query in samples:
        qid = query["qid"]
        keywords = parsed[qid].keywords
        concepts = parsed[qid].concepts
        for operator in ("or", "and"):
            for search_body in (False, True):
                where = where_all if search_body else where_parent
                started = time.monotonic()
                production, error = bounded(PROD_SECONDS, index.search_fts_boolean,
                                            keywords, concepts, operator, filters, POOL, search_body)
                elapsed = time.monotonic() - started
                if production is None:
                    print(f"  {qid} op={operator:<3} body={int(search_body)} 生产={error}"
                          f"（{elapsed:.1f}s）→ 跳过比对", flush=True)
                    continue
                replica = (_legacy_boolean_rows if operator == "or" else _fixed_boolean_rows)(
                    table, concepts, operator, where, POOL, row_count)
                same = ranked(index, production) == ranked(index, replica)
                print(f"  {qid} op={operator:<3} body={int(search_body)} 生产行={len(production):>3} "
                      f"复刻行={len(replica):>3} 一致={same}（{elapsed:.1f}s）", flush=True)

    # 同一查询 body 开关的裸对比（or 分支，生产路径）
    print("  生产 or 分支 body 开关裸对比（同一查询两次调用）:")
    for query in samples:
        qid = query["qid"]
        keywords, concepts = parsed[qid].keywords, parsed[qid].concepts
        off, off_err = bounded(PROD_SECONDS, index.search_fts_boolean,
                               keywords, concepts, "or", filters, POOL, False)
        on, on_err = bounded(PROD_SECONDS, index.search_fts_boolean,
                             keywords, concepts, "or", filters, POOL, True)
        off_n = off_err if off is None else len(off)
        on_n = on_err if on is None else len(on)
        print(f"  {qid} body=0 行={off_n} body=1 行={on_n} 相同={off == on}", flush=True)

    # ---- C. 端到端 service.search(mode='keyword') ----
    print("\nC. 端到端（service.search, mode=keyword）:")

    class _Unused:
        async def embed_query(self, query):  # pragma: no cover - keyword 模式不调用
            raise RuntimeError("provider not used in keyword mode")

        async def rerank_scored(self, query, contents):  # pragma: no cover
            raise RuntimeError("provider not used in keyword mode")

    async def probe() -> None:
        service = HybridSearchService(index=index, embedding_provider=_Unused(),
                                      reranker_provider=_Unused(), search_timeout=PROD_SECONDS)
        for query in samples:
            qid = query["qid"]
            keywords, concepts = parsed[qid].keywords, parsed[qid].concepts
            for operator in ("and", "or"):
                for search_body in (False, True):
                    started = time.monotonic()
                    page = await service.search(keywords, CandidateFilters(), limit=CAP,
                                                mode="keyword", operator=operator,
                                                concepts=concepts, search_body=search_body)
                    print(f"  {qid} op={operator:<3} body={int(search_body)} "
                          f"items={len(page.items):>3} empty={page.empty_reason or '-'} "
                          f"degraded={list(page.degraded_reasons) or '-'} "
                          f"（{time.monotonic() - started:.1f}s）", flush=True)

    asyncio.run(probe())


if __name__ == "__main__":
    main()
