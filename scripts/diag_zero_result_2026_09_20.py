"""诊断：S1–S3 后 5 个零结果岗位的归因（年限窗口 / 关键词收敛 / 方向窄化）。

只读：数据库只读 URI，索引只读，不调 optimize_pending()。
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import Jd, JdRevision  # noqa: E402
from kerui_recruit.match.service import MatchService, _hard_filter, _query_text  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.lexicon import tokenize_lexical_text  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
TARGETS = {"WMT Avaloq 开发工程师", "中级java开发工程师（保险产品方向）",
           "AI研发效能全栈工程师", "中级java开发工程师（营销与客服方向）", "数仓技术TL"}


def old_query(parsed: dict, source_text: str | None) -> str:
    """改动前的 query：required_skills + MUST 技能 requirements + core_duties。"""
    required = " ".join(parsed.get("required_skills", []))
    skill_must = " ".join(
        req.get("value", "") for req in parsed.get("requirements", [])
        if req.get("kind") == "MUST" and req.get("label") in ("技能", "skill"))
    duties = " ".join(str(d) for d in (parsed.get("core_duties") or []))
    return " ".join(p for p in (required, skill_must, duties) if p) or (parsed.get("summary") or source_text or "")


async def main() -> None:
    db = DEV / "db" / "recruit.sqlite3"
    engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true", future=True)
    factory = sessionmaker(bind=engine)
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model="BAAI/bge-m3", schema_version="10", chunk_version="8")
    search = HybridSearchService(index=index, embedding_provider=None,
                                 reranker_provider=None, search_timeout=120)
    matcher = MatchService(session_factory=factory, search_service=search)

    with factory() as session:
        rows = session.execute(
            select(JdRevision.id, Jd.title, JdRevision.source_text)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   projection_is_current("jd", Jd.id))
        ).all()

    for revision_id, title, source_text in rows:
        if title not in TARGETS:
            continue
        parsed = matcher._revision(revision_id).parsed_data or {}
        context = matcher._revision(revision_id)
        filters = _hard_filter(context, None)
        new_q = _query_text(context)
        old_q = old_query(parsed, source_text)

        async def count(query, flt):
            page = await search.search(query, flt, limit=70, mode="keyword")
            return len(page.items)

        new_all = await count(new_q, filters)
        old_all = await count(old_q, filters)
        no_window = await count(new_q, replace(filters, min_years=None, max_years=None))
        no_direction = await count(new_q, replace(filters, career_directions=(), career_specializations=()))
        neither = await count(new_q, replace(filters, min_years=None, max_years=None,
                                             career_directions=(), career_specializations=()))
        print(f"\n《{title}》")
        print(f"  窗口={filters.min_years}~{filters.max_years} "
              f"方向细分={filters.career_specializations} 大类={filters.career_directions}")
        print(f"  新 query token={len(tokenize_lexical_text(new_q))} / 旧 query token={len(tokenize_lexical_text(old_q))}")
        print(f"  新 query + 全部硬条件     → {new_all}")
        print(f"  旧 query + 全部硬条件     → {old_all}   ← 改动前的口径")
        print(f"  新 query + 去掉年限窗口   → {no_window}")
        print(f"  新 query + 去掉方向窄化   → {no_direction}")
        print(f"  新 query + 两者都去掉     → {neither}")


if __name__ == "__main__":
    asyncio.run(main())
