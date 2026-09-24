"""§8 补充：`match_jd`（岗位匹配人）的真实数据端到端验证。

为什么单独写：本轮改了匹配路径（硬条件下推 + 安全阀 + `SearchPage` 新增
`hard_filters` / `relaxed`），需要证明它在真实库上跑得通、且不破坏原有行为。

前置事实（本轮普查结论）：当前 80 个 READY JD 的 `exact_constraints` **全为空**，
所以本轮的下推在这批数据上是**空转** —— 本脚本同时验证这一点
（`hard_filters` 应为空、`relaxed` 应为空、结果与改动前同构）。

安全：数据库以 **只读 URI** 打开；只读 `.dev-data/search`；不调用 `optimize_pending()`；
候选人 ID 用 sha256 别名。
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
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import Jd, JdRevision  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.match.service import MatchService  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-plan" / "e2e_match.json"
JDS = 12
LIMIT = 50


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


async def main() -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])

    db = DEV / "db" / "recruit.sqlite3"
    engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true", future=True)
    factory = sessionmaker(bind=engine)

    with factory() as session:
        rows = session.execute(
            select(JdRevision.id, Jd.company, Jd.title)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   projection_is_current("jd", Jd.id))
            .limit(JDS)
        ).all()
    print(f"可用 JD（OPEN + 当前 READY 投影）= {len(rows)}")

    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                              embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"候选人索引不可读：{index.read_compatibility_error}")

    report = []
    async with httpx.AsyncClient(timeout=120) as client:
        embedding = SiliconFlowEmbeddingProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(
            api_key=key, client=client, base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_reranker_model"])
        search = HybridSearchService(index=index, embedding_provider=embedding,
                                     reranker_provider=reranker, search_timeout=120)
        matcher = MatchService(session_factory=factory, search_service=search, jd_index=None)

        for revision_id, company, title in rows:
            started = time.monotonic()
            page = await matcher.match_jd(revision_id=revision_id, limit=LIMIT, mode="hybrid")
            elapsed = (time.monotonic() - started) * 1000
            cell = {
                "jd": f"{company or '-'}/{title or '-'}",
                "返回条数": len(page.items),
                "empty_reason": page.empty_reason,
                "降级": list(page.degraded_reasons),
                "hard_filters": list(page.hard_filters),
                "relaxed": list(page.relaxed),
                "耗时ms": round(elapsed, 1),
                "前5": [alias(hit.candidate_id) for hit in page.items[:5]],
            }
            report.append(cell)
            print(f"  {cell['jd'][:34]:<34} 返回={cell['返回条数']:<4} "
                  f"硬条件={len(cell['hard_filters'])} 放宽={len(cell['relaxed'])} "
                  f"降级={cell['降级']} {cell['耗时ms']}ms", flush=True)

    counts = [c["返回条数"] for c in report]
    latencies = [c["耗时ms"] for c in report]
    print(f"\n返回条数 min/median/max = {min(counts)}/{statistics.median(counts)}/{max(counts)}")
    print(f"零结果 JD = {sum(1 for c in report if c['返回条数'] == 0)}/{len(report)}")
    print(f"耗时 P50/P95 = {statistics.median(latencies):.0f}ms / "
          f"{sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]:.0f}ms")
    print(f"下推硬条件总数 = {sum(len(c['hard_filters']) for c in report)}"
          f"（预期 0：当前 JD 的 exact_constraints 全空）")
    print(f"安全阀触发 = {sum(1 for c in report if c['relaxed'])}（预期 0）")
    print(f"重排降级 = {sum(1 for c in report if c['降级'])}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"产物={OUT}")


if __name__ == "__main__":
    asyncio.run(main())
