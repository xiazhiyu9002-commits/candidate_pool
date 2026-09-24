"""§1 第 1 步：跑「模糊问法 + 开启查询改写」的真实检索。

与既有基线可比：基线取自 ``.tmp-judge/search_results.json`` 的 ``vague`` 档
（由 ``judge_eval_run_searches_2026_09_19.py`` 生成，`limit=100`、`rewrite_enabled=False`）。
本脚本只改一个变量 —— 打开改写，其余（问法、模式、limit、取前 10）完全一致。

改写器用**生产同一实现**：`SemanticQueryRewriter`（含同一 system prompt 与
「改写不得引入硬筛选」校验），底层模型按 `.dev-data/config/ai-providers.json` 里
FAST_TEXT 角色的实际配置（DeepSeek / deepseek-flash）。

产物：
- ``.tmp-plan/rewrite_on.json``       结构与基线一致，便于直接对比
- ``.tmp-plan/rewrite_on_diag.json``  改写状态、降级原因、耗时

安全：只读 ``.dev-data``；不打印密钥；候选人 ID 用 sha256 别名。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402

from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.openai_compatible import OpenAICompatibleClient  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.rewrite import SemanticQueryRewriter  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
JUDGE = ROOT / ".tmp-judge"
OUT = ROOT / ".tmp-plan"
MODES = ("vector", "hybrid")
TOP_K = 10
LIMIT = 100


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class CachingEmbedding:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    async def embed_documents(self, texts):
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str):
        self.calls += 1
        return await self.inner.embed_query(text)


def rewrite_model() -> str:
    """取 FAST_TEXT 角色的实际模型名（QUERY_REWRITE 走的就是这个角色）。"""
    path = DEV / "config" / "ai-providers.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        for connection in config.get("connections") or []:
            models = (connection or {}).get("models") or {}
            if connection.get("enabled") and models.get("fast_text"):
                return str(models["fast_text"])
    except Exception:  # noqa: BLE001 - 配置缺失时回落到默认
        pass
    return "deepseek-flash"


async def main() -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key"))
    sf_key = key.decrypt(settings["siliconflow_api_key"])
    ds_key = key.decrypt(settings["deepseek_api_key"])
    model = rewrite_model()
    print(f"改写器：SemanticQueryRewriter + [{model}]（FAST_TEXT 角色）")

    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    jds = json.loads((JUDGE / "jd_selected.json").read_text(encoding="utf-8"))
    queries = json.loads((JUDGE / "queries.json").read_text(encoding="utf-8"))

    results: dict = {}
    diagnostics: dict = {}
    latencies: list[float] = []

    async with httpx.AsyncClient(timeout=120) as client:
        embedding = CachingEmbedding(SiliconFlowEmbeddingProvider(
            api_key=sf_key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"]))
        reranker = SiliconFlowRerankerProvider(
            api_key=sf_key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_reranker_model"])
        rewriter = SemanticQueryRewriter(OpenAICompatibleClient(
            base_url=settings.get("deepseek_base_url") or "https://api.deepseek.com",
            api_key=ds_key, model=model, http_client=client))
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=reranker, rewriter=rewriter,
                                      search_timeout=120)

        for i, jd in enumerate(jds, 1):
            jid = f"J{i:02d}"
            text = queries[jid]["vague"]
            results[jid] = {}
            diagnostics[jid] = {}
            for mode in MODES:
                started = time.monotonic()
                page = await service.search(text, CandidateFilters(), limit=LIMIT,
                                            mode=mode, rewrite_enabled=True)
                latencies.append((time.monotonic() - started) * 1000)
                order = [alias(hit.candidate_id) for hit in page.items[:TOP_K]]
                results[jid][mode] = order
                plan = page.query_plan
                diagnostics[jid][mode] = {
                    "rewrite_status": getattr(plan, "rewrite_status", None),
                    "semantic_query": getattr(plan, "semantic_query", None),
                    "returned": len(page.items),
                    "degraded": list(page.degraded_reasons),
                    "empty_reason": page.empty_reason,
                }
            print(f"[{i:>2}/{len(jds)}] {jid} {str(jd.get('title'))[:24]}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rewrite_on.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "rewrite_on_diag.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=1), encoding="utf-8")

    statuses = [d["rewrite_status"] for jd in diagnostics.values() for d in jd.values()]
    success = sum(1 for s in statuses if s == "success")
    unavailable = sum(1 for s in statuses if s == "unavailable")
    degraded = sum(1 for jd in diagnostics.values() for d in jd.values() if d["degraded"])
    print(f"\n改写状态：success={success} unavailable={unavailable} 其他={len(statuses) - success - unavailable}")
    print(f"重排降级次数={degraded}")
    print(f"耗时 P50={statistics.median(latencies):.0f}ms "
          f"P95={sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]:.0f}ms")
    print("改写生效样例：")
    for jid in list(diagnostics)[:5]:
        d = diagnostics[jid]["hybrid"]
        print(f"  {jid}: {d['rewrite_status']} → {str(d['semantic_query'])[:80]}")
    print(f"\n查询向量 API 调用={embedding.calls}  产物={OUT / 'rewrite_on.json'}")


if __name__ == "__main__":
    asyncio.run(main())
