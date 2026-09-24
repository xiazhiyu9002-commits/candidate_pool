"""诊断：WMT Avaloq 开发工程师 召回 70 但输出 0 的原因（资格淘汰 vs 分数不足）。"""
from __future__ import annotations

import asyncio
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import Jd, JdRevision  # noqa: E402
from kerui_recruit.match.service import MatchService, _hard_filter, _query_text  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"


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
        row = session.execute(
            select(JdRevision.id, Jd.title, JdRevision.min_years, JdRevision.highest_degree)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   Jd.title == "WMT Avaloq 开发工程师")
        ).first()
    revision_id, title, min_years, degree = row
    context = matcher._revision(revision_id)
    parsed = context.parsed_data or {}
    print(f"《{title}》 min_years={min_years} highest_degree={degree}")
    print(f"  required_skills={parsed.get('required_skills')}")
    print(f"  must_skill_groups={parsed.get('must_skill_groups')}")
    print(f"  career_directions={parsed.get('career_directions')} "
          f"specializations={parsed.get('career_specializations')} "
          f"business={parsed.get('business_directions')}")

    filters = _hard_filter(context, None)
    page = await search.search(_query_text(context), filters, limit=70, mode="keyword")
    print(f"\n  召回 = {len(page.items)}")

    from kerui_recruit.match.policy import evaluate_pair

    data_map = matcher._candidate_parsed_data([hit.candidate_id for hit in page.items])
    rejected = collections.Counter()
    scores = []
    for hit in page.items:
        data = data_map.get(hit.candidate_id, {})
        decision = evaluate_pair(parsed, data)
        if decision.eligibility == "rejected":
            rejected[decision.hard_reasons] += 1
            continue
        scores.append(matcher._score_context(context, hit, data).total)
    print(f"  资格淘汰 = {sum(rejected.values())}  {dict(rejected)}")
    print(f"  通过资格 = {len(scores)}；分数 min/max = "
          f"{(min(scores) if scores else None)}/{(max(scores) if scores else None)}"
          f"；≥0.4 的 = {sum(1 for s in scores if s >= 0.4)}")


if __name__ == "__main__":
    asyncio.run(main())
