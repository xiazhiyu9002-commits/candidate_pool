"""判决式评测第 1 步：用真实检索管线跑 20 JD × 3 问法 × 3 模式，各取前 10。

与生产一致性：
- ``limit=100``（前端默认值），因此重排候选池大小与线上一致；取前 10 用于判决。
- ``rewrite_enabled=False``（前端默认值，且 keyword 模式禁用改写）。
- 查询文本以**原文**进入管线：FTS 侧由 ``parse_query`` 解析、向量侧直接嵌入原文。

产物 ``.tmp-judge/search_results.json``（候选人用 sha256 别名）。
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

from pydantic import SecretStr  # noqa: E402

from kerui_recruit.core.settings import Settings  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.factory import build_providers  # noqa: E402
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-judge"
MODES = ("keyword", "vector", "hybrid")
PHRASINGS = ("standard", "colloquial", "vague")
TOP_K = 10
LIMIT = 100


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


async def main() -> None:
    raw = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(raw["siliconflow_api_key"])
    settings = Settings(data_root=DEV, session_token="judge-eval",
                        siliconflow_api_key=SecretStr(key))
    bundle = build_providers(settings)
    index = LanceDBSearchIndex(
        DEV / "search",
        vector_dimension=bundle.vector_dimension,
        embedding_model=raw["siliconflow_embedding_model"],
    )
    service = HybridSearchService(
        index=index, embedding_provider=bundle.embedding, reranker_provider=bundle.reranker,
    )

    jds = json.loads((OUT / "jd_selected.json").read_text(encoding="utf-8"))
    queries = json.loads((OUT / "queries.json").read_text(encoding="utf-8"))
    results: dict[str, dict] = {}
    diagnostics: dict[str, dict] = {}
    started = time.monotonic()

    for i, jd in enumerate(jds, 1):
        jid = f"J{i:02d}"
        results[jid] = {}
        diagnostics[jid] = {}
        for phrasing in PHRASINGS:
            text = queries[jid][phrasing]
            results[jid][phrasing] = {}
            diagnostics[jid][phrasing] = {}
            for mode in MODES:
                page = await service.search(
                    text, CandidateFilters(), limit=LIMIT, mode=mode, rewrite_enabled=False
                )
                order = [alias(hit.candidate_id) for hit in page.items[:TOP_K]]
                results[jid][phrasing][mode] = order
                diagnostics[jid][phrasing][mode] = {
                    "returned": len(page.items),
                    "degraded": list(page.degraded_reasons),
                    "empty_reason": page.empty_reason,
                }
        elapsed = time.monotonic() - started
        print(f"[{i:>2}/{len(jds)}] {jid} {jd['company'][:10]}/{jd['title'][:22]} "
              f"用时 {elapsed:.0f}s", flush=True)

    (OUT / "search_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "search_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=1), encoding="utf-8")

    degraded = sum(1 for j in diagnostics.values() for p in j.values()
                   for d in p.values() if d["degraded"])
    empties = sum(1 for j in diagnostics.values() for p in j.values()
                  for d in p.values() if d["returned"] == 0)
    print(f"\n完成：{len(jds) * len(PHRASINGS) * len(MODES)} 次检索，"
          f"降级={degraded}，零结果={empties}")
    for mode in MODES:
        sizes = [len(set(r[mode])) for j in results.values() for r in j.values()]
        print(f"  {mode:<8} 每次返回去重后中位={sorted(sizes)[len(sizes)//2]}")
    if bundle.http_client is not None:
        await bundle.http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
