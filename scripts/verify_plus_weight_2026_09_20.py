"""PLUS 加权（preference 分量）的实测影响：有/无优先项加权，召回数与排序如何变化。

背景：`skill` / `other_keyword` 的 MUST 已降级为 PLUS（不再淘汰人），精度靠
`match/service.py::_score_context` 的 `preference` 分量（权重 `_PREFERENCE_WEIGHT`）补回来。
本脚本用同一批 JD 跑两遍——一遍带 preference，一遍把 preference 抹掉（等价于改动前）——
直接量化「加权后少召回多少人、排序挪动了多少」，避免只看单元测试就下结论。

安全：数据库只读 URI；只读索引；不调 optimize_pending()；不写数据库；不调用外部 API
（keyword 模式 + 无 rerank/embedding provider，纯确定性）。
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

import kerui_recruit.match.policy as policy  # noqa: E402
from kerui_recruit.db.models import Jd, JdRevision  # noqa: E402
from kerui_recruit.match.jd_index import JdSearchIndex  # noqa: E402
from kerui_recruit.match.service import MatchService  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
LIMIT = 50
MAX_JDS = 8


def _plus_count(parsed: dict) -> int:
    return sum(
        1 for c in (parsed.get("exact_constraints") or [])
        if isinstance(c, dict) and str(c.get("strength") or "").upper() == "PLUS"
    )


class _WithoutPreference:
    """上下文管理器：抹掉 PairDecision.preference，等价于「优先项不加权」的旧行为。"""

    def __enter__(self) -> None:
        self._original = policy.evaluate_pair
        original = self._original

        def stripped(jd_parsed, candidate_parsed):
            decision = original(jd_parsed, candidate_parsed)
            if decision.preference is None:
                return decision
            return replace(decision, preference=None)

        policy.evaluate_pair = stripped

    def __exit__(self, *exc) -> None:
        policy.evaluate_pair = self._original


async def main() -> None:
    db = DEV / "db" / "recruit.sqlite3"
    engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true", future=True)
    factory = sessionmaker(bind=engine)
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=1024,
                               embedding_model="BAAI/bge-m3", schema_version="10", chunk_version="8")
    jd_index = JdSearchIndex(DEV / "search" / "jobs", vector_dimension=1024,
                             embedding_model="BAAI/bge-m3", schema_version="10", chunk_version="8")
    search = HybridSearchService(index=index, embedding_provider=None,
                                 reranker_provider=None, search_timeout=120)
    matcher = MatchService(session_factory=factory, search_service=search, jd_index=jd_index)

    with factory() as session:
        rows = session.execute(
            select(JdRevision.id, Jd.title, JdRevision.parsed_data)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   projection_is_current("jd", Jd.id))
        ).all()

    targets = []
    for revision_id, title, parsed in rows:
        try:
            data = json.loads(parsed) if isinstance(parsed, str) else (parsed or {})
        except Exception:
            data = {}
        targets.append((revision_id, title, _plus_count(data)))
    targets.sort(key=lambda item: -item[2])
    targets = [t for t in targets if t[2] > 0][:MAX_JDS] or targets[:MAX_JDS]

    print(f"=== PLUS 加权的实测影响（keyword 模式，limit={LIMIT}）===")
    print(f"{'岗位':<28}{'PLUS项':<8}{'带加权':<8}{'无加权':<8}{'净变化':<8}{'掉出0.4':<10}{'Top1 变化'}")
    dropped_total = 0
    for revision_id, title, plus_n in targets:
        with_pref = await matcher.match_jd(revision_id=revision_id, limit=LIMIT, mode="keyword")
        with_ids = [hit.candidate_id for hit in with_pref.items]

        with _WithoutPreference():
            without_pref = await matcher.match_jd(revision_id=revision_id, limit=LIMIT, mode="keyword")
        without_ids = [hit.candidate_id for hit in without_pref.items]

        dropped = [cid for cid in without_ids if cid not in set(with_ids)]
        dropped_total += len(dropped)
        top_changed = "是" if with_ids[:1] != without_ids[:1] else "-"
        print(f"{str(title)[:26]:<28}{plus_n:<8}{len(with_ids):<8}{len(without_ids):<8}"
              f"{len(with_ids) - len(without_ids):<8}{len(dropped):<10}{top_changed}")

    print(f"\n合计因加权掉出阈值的人次：{dropped_total}")
    print("注：掉出阈值 = 这些人的总分被优先项稀释后低于 _MIN_SCORE(0.4)，不是被硬拒。")


if __name__ == "__main__":
    asyncio.run(main())
