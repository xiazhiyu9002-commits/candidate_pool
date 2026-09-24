"""S1–S3 验收：真实库端到端（关键词收敛 / 年限硬窗口 / 业务方向按比例置顶）。

对应方案 `.trae/documents/检索与匹配重构方案-2026-09-20.md` §6.3。

- 关键词模式（不调外部 API）跑全部活跃 JD，验证返回数、窗口生效、80/20 配额；
- 混合模式抽样 3 个 JD，验证真实链路（需可用 API key，失败则记录并跳过）；
- 反向匹配抽样 1 人，验证同一套门槛与排序在反向上成立。

安全：数据库 **只读 URI**；只读 `.dev-data/search`；不调用 `optimize_pending()`；
候选人/岗位用 sha256 别名输出。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision  # noqa: E402
from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.match.keywords import (  # noqa: E402
    build_jd_biz_terms,
    build_jd_query_text,
    build_jd_tech_terms,
)
from kerui_recruit.match.service import MatchService, _query_text, _years_window  # noqa: E402
from kerui_recruit.match.jd_index import JdSearchIndex  # noqa: E402
from kerui_recruit.providers.siliconflow import (  # noqa: E402
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.lexicon import tokenize_lexical_text  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
LIMIT = 50


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def ordering_violations(flags: list[bool | None]) -> int:
    """置顶违规：出现「非一致者排在一致者前面」的次数（= 尾部一致者数量）。"""
    seen_other = False
    violations = 0
    for flag in flags:
        if flag is True:
            if seen_other:
                violations += 1
        else:
            seen_other = True
    return violations


async def run_mode(matcher, rows, mode: str) -> list[dict]:
    report = []
    for index, (revision_id, title, min_years) in enumerate(rows, start=1):
        page = await matcher.match_jd(revision_id=revision_id, limit=LIMIT, mode=mode)
        hits = list(page.items)
        flags = [matcher.score(revision_id, hit).business_match for hit in hits]
        window = _years_window(float(min_years) if min_years is not None else None)
        report.append({
            "jd": title, "min_years": float(min_years) if min_years is not None else None,
            "returned": len(hits), "window": window,
            "preferred": sum(1 for flag in flags if flag is True),
            "violations": ordering_violations(flags),
            "query_tokens": len(tokenize_lexical_text(_query_text(matcher._revision(revision_id)))),
            "empty_reason": page.empty_reason, "degraded": list(page.degraded_reasons),
            "years": [hit.total_years for hit in hits],
        })
        cell = report[-1]
        win_text = f"{window[0]:g}~{window[1]:g}" if window else "—"
        print(f"  [{index:>2}/{len(rows)}] {str(title)[:26]:<28}"
              f"{(cell['min_years'] if cell['min_years'] is not None else '—'):>5} "
              f"{win_text:>9} 返回={cell['returned']:<4} 业务一致={cell['preferred']:<4} "
              f"违规={cell['violations']} token={cell['query_tokens']:<4} "
              f"降级={cell['degraded']}", flush=True)
    return report


def summarize(report: list[dict]) -> None:
    counts = [c["returned"] for c in report]
    strict = [c for c in report if c["returned"] == LIMIT]
    print(f"  返回数 min/中位/max = {min(counts)}/{statistics.median(counts):.0f}/{max(counts)}")
    print(f"  返回数 <10 的 JD = {sum(1 for c in counts if c < 10)} 个 → "
          f"{[(c['jd'], c['returned']) for c in report if c['returned'] < 10]}")
    print(f"  零结果 = {sum(1 for c in counts if c == 0)} 个；"
          f"降级 = {sum(1 for c in report if c['degraded'])} 个")
    print(f"  置顶违规（非一致者排在一致者前面）= {sum(c['violations'] for c in report)} 处（预期 0）")
    print(f"  配额：返回满 {LIMIT} 条的 {len(strict)} 个 JD 中，一致者 ≥40 的占 "
          f"{sum(1 for c in strict if c['preferred'] >= 40)} 个（这些应前 40 全为一致者）")
    violations = []
    for cell in report:
        if not cell["window"]:
            continue
        low, high = cell["window"]
        for years in cell["years"]:
            if years is None or not (low <= years <= high):
                violations.append((cell["jd"], years, cell["window"]))
    print(f"  窗口越界记录 = {len(violations)} 条（预期 0）")


async def main() -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    db = DEV / "db" / "recruit.sqlite3"
    engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true", future=True)
    factory = sessionmaker(bind=engine)

    with factory() as session:
        rows = session.execute(
            select(JdRevision.id, Jd.title, JdRevision.min_years)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   projection_is_current("jd", Jd.id))
        ).all()
    print(f"活跃 JD = {len(rows)}\n")

    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model="BAAI/bge-m3", schema_version="10", chunk_version="8")
    if not index.is_ready():
        raise SystemExit(f"候选人索引不可读：{index.read_compatibility_error}")
    jd_index = JdSearchIndex(DEV / "search" / "jobs", vector_dimension=1024,
                             embedding_model="BAAI/bge-m3", schema_version="10", chunk_version="8")

    keyword_search = HybridSearchService(index=index, embedding_provider=None,
                                         reranker_provider=None, search_timeout=120)
    keyword_matcher = MatchService(session_factory=factory, search_service=keyword_search,
                                   jd_index=jd_index)

    print("=== 1. 关键词模式：全部活跃 JD（无外部 API，快速对照）===")
    keyword_report = await run_mode(keyword_matcher, rows, "keyword")
    summarize(keyword_report)
    empty = [(c["jd"], c["empty_reason"]) for c in keyword_report if c["returned"] == 0]
    if empty:
        print(f"  零结果原因：{empty}")

    print("\n=== 2. 关键词样例（技术 / 业务两类）===")
    with factory() as session:
        for revision_id, title, _ in rows[:3]:
            parsed = session.get(JdRevision, revision_id).parsed_data or {}
            print(f"  《{title}》")
            print(f"    技术词：{build_jd_tech_terms(parsed)}")
            print(f"    业务词：{build_jd_biz_terms(parsed)}")
            print(f"    query：{build_jd_query_text(parsed)[:160]}")

    print("\n=== 3. 混合模式：全部活跃 JD（生产口径，需 API）===")
    hybrid_report = None
    try:
        key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
            settings["siliconflow_api_key"])
        async with httpx.AsyncClient(timeout=180) as client:
            embedding = SiliconFlowEmbeddingProvider(
                api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                model=settings["siliconflow_embedding_model"])
            reranker = SiliconFlowRerankerProvider(
                api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                model=settings["siliconflow_reranker_model"])
            hybrid_search = HybridSearchService(index=index, embedding_provider=embedding,
                                                reranker_provider=reranker, search_timeout=180)
            hybrid_matcher = MatchService(session_factory=factory, search_service=hybrid_search,
                                          jd_index=jd_index)
            hybrid_report = await run_mode(hybrid_matcher, rows, "hybrid")
            summarize(hybrid_report)
    except Exception as error:  # noqa: BLE001 - 验收脚本需报告而非中断
        print(f"  混合模式跳过：{type(error).__name__}: {error}")

    print("\n=== 4. 反向匹配抽样（人 → 岗位）===")
    with factory() as session:
        person = session.execute(
            select(Candidate.id, Candidate.display_name, Candidate.total_years)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
            .limit(3)
        ).all()
    for candidate_id, name, years in person:
        records = await keyword_matcher.reverse_match_candidate(candidate_id, limit=LIMIT, mode="keyword")
        flags = [r.score.business_match for r in records]
        print(f"  {alias(candidate_id)} 年限={years} 返回={len(records)} "
              f"业务一致={sum(1 for f in flags if f is True)} 置顶违规={ordering_violations(flags)}")
        for record in records[:3]:
            print(f"      {str(record.title)[:26]:<28} 总分={record.score.total} "
                  f"业务一致={record.score.business_match} 年限={record.hit.total_years}")

    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
