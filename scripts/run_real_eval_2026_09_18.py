"""在真实库（.dev-data）上跑 keyword / vector / hybrid 三模式检索。

输入：``.tmp-real-eval/real_queries.json``（由 build_real_query_set_2026_09_18.py 生成）。
输出：``.tmp-real-eval/retrieval/real_<variant>.json``（``{qid: {"intent":..., "<variant>": [alias,...]}}``）
     与 ``.tmp-real-eval/retrieval/real_<variant>.meta.json``（降级原因/条数诊断）。

变体（``VARIANTS``）：
- ``keyword`` / ``vector`` / ``hybrid``：生产行为（向量通道搜全部 chunk）。
- ``vector_parent`` / ``hybrid_parent``：**向量通道限定 ``chunk_type='parent'``**。
  索引里 child 占 92%（20491/22213），而生产 ``search_vector`` 不做 chunk 类型过滤，
  因此需要这一对照来隔离「child 进入候选池」的净效果。

安全约定：
- **只读**直连 ``.dev-data/search`` 与 ``.dev-data/db/recruit.sqlite3``；
- **绝不**调用 ``optimize_pending()``（会 ``.optimize()`` 并删除 ``.fts-dirty``，写库）；
- 不打印密钥；产物全部为别名 ID。

口径：不使用 JD 硬条件过滤（filters 为空），先测「纯检索」能力；带过滤对照另行实验。
查询文本走生产同一条 ``parse_query`` 解析路径，取 ``keywords`` 作为检索词。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.query import parse_query  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL_DIR = ROOT / ".tmp-real-eval"
OUT_DIR = EVAL_DIR / "retrieval"

# 变体 -> (service 选择键, service 的 mode 参数)
VARIANTS: dict[str, tuple[str, str]] = {
    "keyword": ("plain", "keyword"),
    "vector": ("plain", "vector"),
    "hybrid": ("plain", "hybrid"),
    "vector_parent": ("parent", "vector"),
    "hybrid_parent": ("parent", "hybrid"),
}
MODES = tuple(VARIANTS)


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class _TracedProvider:
    """仅用于评测：打印生产代码里被静默吞掉的 provider 异常，并按需退避重试。

    生产 ``search()`` 把 rerank 异常吞成 ``RERANKER_UNAVAILABLE`` 降级原因，评测时
    必须看到真实原因（实测为上游 ``E_API_RATE_LIMIT``）。重试需要比生产 8s 更宽的
    ``search_timeout``，否则重试会被 ``_until`` 取消。
    """

    def __init__(self, inner, method: str, attempts: int = 1, base_delay: float = 6.0) -> None:
        self._inner = inner
        self._method = method
        self._attempts = attempts
        self._base_delay = base_delay

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def _call(self, *args):
        function = getattr(self._inner, self._method)
        last: Exception | None = None
        for attempt in range(self._attempts):
            started = time.monotonic()
            try:
                return await function(*args)
            except Exception as exc:
                last = exc
                print(f"    {self._method} ERROR {time.monotonic() - started:.2f}s "
                      f"attempt={attempt + 1}/{self._attempts} "
                      f"{type(exc).__name__}: {str(exc)[:160]}", flush=True)
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(self._base_delay * (attempt + 1))
        raise last

    async def rerank_scored(self, query, contents):
        return await self._call(query, contents)

    async def embed_query(self, query):
        return await self._call(query)


class _ParentOnlyVectorIndex(LanceDBSearchIndex):
    """仅评测用：把向量通道限定在 ``chunk_type='parent'``。

    生产 ``search_vector`` 不对 chunk 类型做任何过滤，因此索引里 92% 的 child
    （单段经历/项目正文）都能进入候选池并成为候选人的代表行。本子类只覆盖向量
    召回、其余行为与生产一致，用于隔离「child 进入候选池」的净效果。
    """

    def search_vector(self, query_vector: tuple[float, ...], filters: CandidateFilters,
                      limit: int) -> list[dict[str, Any]]:
        self._require_readable()
        if not self._table_exists() or not query_vector:
            return []
        table = self.database.open_table(self.table_name)
        if table.count_rows() == 0:
            return []
        where = self._where(filters)
        where = " AND ".join(c for c in (where, "chunk_type = 'parent'") if c)
        builder = table.search(list(query_vector), vector_column_name="vector", query_type="vector")
        if where:
            builder = builder.where(where, prefilter=True)
        return self._candidate_rows(builder, table.count_rows(), limit, filters)


def load_queries() -> list[dict]:
    path = EVAL_DIR / "real_queries.json"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，先运行 build_real_query_set_2026_09_18.py")
    return json.loads(path.read_text(encoding="utf-8"))


def load_done(mode: str) -> dict:
    path = OUT_DIR / f"real_{mode}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


async def run_mode(service, variant: str, mode: str, queries: list[dict], limit: int,
                   redo_degraded: bool) -> None:
    done = load_done(variant)
    meta_path = OUT_DIR / f"real_{variant}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if redo_degraded:
        retry = [qid for qid, item in meta.items() if item.get("degraded")]
        for qid in retry:
            done.pop(qid, None)
            meta.pop(qid, None)
        print(f"[{variant}] 重跑降级查询={len(retry)}", flush=True)
    pending = [q for q in queries if q["qid"] not in done]
    print(f"[{variant}] 已完成={len(done)} 待跑={len(pending)}", flush=True)
    degraded_total: dict[str, int] = {}
    empties = 0
    for query in pending:
        parsed = parse_query(query["text"])
        filters = CandidateFilters()
        page = await service.search(parsed.keywords, filters, limit=limit, mode=mode)
        ids: list[str] = []
        seen: set[str] = set()
        for hit in page.items:
            key = alias(hit.candidate_id)
            if key not in seen:
                seen.add(key)
                ids.append(key)
        done[query["qid"]] = {"intent": query.get("title") or "", variant: ids}
        meta[query["qid"]] = {
            "n": len(ids),
            "degraded": list(page.degraded_reasons),
            "empty_reason": page.empty_reason,
            "keywords_len": len(parsed.keywords),
        }
        for reason in page.degraded_reasons:
            degraded_total[reason] = degraded_total.get(reason, 0) + 1
        if not ids:
            empties += 1
        print(f"  {query['qid']} n={len(ids)} degraded={list(page.degraded_reasons) or '-'}",
              flush=True)
        (OUT_DIR / f"real_{variant}.json").write_text(
            json.dumps(done, ensure_ascii=False), encoding="utf-8")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{variant}] 完成 空结果={empties} 降级={degraded_total or '无'}", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--search-timeout", type=float, default=8.0)
    parser.add_argument("--redo-degraded", action="store_true")
    parser.add_argument("--trace-errors", action="store_true")
    parser.add_argument("--retry", type=int, default=0, help="provider 退避重试次数（评测用）")
    args = parser.parse_args()
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        raise SystemExit(f"未知模式 {unknown}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    queries = load_queries()
    print(f"真实查询={len(queries)} 模式={modes} limit={args.limit}")

    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    parent_index = _ParentOnlyVectorIndex(DEV / "search", vector_dimension=1024,
                                          embedding_model=settings["siliconflow_embedding_model"])
    print(f"index is_ready={index.is_ready()} is_compatible={index.is_compatible()} "
          f"(旧索引：可读不可写；本脚本不调用 optimize_pending)")
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    async with httpx.AsyncClient(timeout=60) as client:
        embedding = SiliconFlowEmbeddingProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])
        trace = args.trace_errors or args.retry > 0
        attempts = max(1, args.retry)
        embedding_provider = _TracedProvider(embedding, "embed_query", attempts) if trace else embedding
        reranker_provider = _TracedProvider(reranker, "rerank_scored", attempts) if trace else reranker
        services = {
            "plain": HybridSearchService(index=index, embedding_provider=embedding_provider,
                reranker_provider=reranker_provider, search_timeout=args.search_timeout),
            "parent": HybridSearchService(index=parent_index, embedding_provider=embedding_provider,
                reranker_provider=reranker_provider, search_timeout=args.search_timeout),
        }
        print(f"models embedding={embedding.model} reranker={reranker.model} "
              f"search_timeout={args.search_timeout} retry={attempts - 1} trace={trace}")
        for variant in modes:
            service_key, mode = VARIANTS[variant]
            await run_mode(services[service_key], variant, mode, queries, args.limit,
                           args.redo_degraded)


if __name__ == "__main__":
    asyncio.run(main())
