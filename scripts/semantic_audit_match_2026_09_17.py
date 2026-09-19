"""Run read-only forward and reverse matching against the frozen audit snapshot."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.match.jd_index import JdSearchIndex
from kerui_recruit.match.service import MatchService
from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider, SiliconFlowRerankerProvider
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"
JDS = ("c8cc7ea1", "d13894a1", "64994b1c", "69111649", "56f942a1",
       "d8504776", "a0675f51", "2ecec438", "543afa81", "399e2a20")
PEOPLE = ("7d5bf75cce11", "9eeb2bf3737c", "f96cb81456e3", "3f5da92e8746", "42c0b000fc46")


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


async def main() -> None:
    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    revisions = {alias(rid)[:8]: rid for (rid,) in connection.execute(
        "SELECT r.id FROM jd_revision r JOIN jd j ON j.id=r.jd_id WHERE r.is_current=1 AND j.deleted_at IS NULL")}
    people = {alias(cid): cid for (cid,) in connection.execute("SELECT id FROM candidate")}
    connection.close()
    engine = create_engine("sqlite+pysqlite:///" + (SNAPSHOT / "recruit.sqlite3").as_posix(),
                           connect_args={"check_same_thread": False})
    factory = sessionmaker(engine, expire_on_commit=False)
    async with httpx.AsyncClient(timeout=40) as client:
        embedding = SiliconFlowEmbeddingProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])
        index = LanceDBSearchIndex(SNAPSHOT / "search", vector_dimension=1024,
                                    embedding_model=settings["siliconflow_embedding_model"])
        jd_index = JdSearchIndex(SNAPSHOT / "search/jobs", vector_dimension=1024,
                                  embedding_model=settings["siliconflow_embedding_model"])
        search = HybridSearchService(index=index, embedding_provider=embedding,
                                     reranker_provider=reranker, search_timeout=8)
        matcher = MatchService(session_factory=factory, search_service=search, jd_index=jd_index)
        result: dict = {"forward": {}, "reverse": {}}
        for jd in JDS:
            if jd not in revisions:
                continue
            modes = {}
            for mode in ("keyword", "vector", "hybrid"):
                page = await matcher.match_jd(revision_id=revisions[jd], limit=100, mode=mode)
                modes[mode] = {"ids": [alias(item.candidate_id) for item in page.items],
                    "empty_reason": page.empty_reason, "degraded": list(page.degraded_reasons)}
            result["forward"][jd] = modes
            (SNAPSHOT / "match_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("JD", jd, {k: (len(v["ids"]), v["empty_reason"], v["degraded"]) for k, v in modes.items()}, flush=True)
        for person in PEOPLE:
            if person not in people:
                continue
            modes = {}
            for mode in ("keyword", "vector", "hybrid"):
                try:
                    rows = await matcher.reverse_match_candidate(people[person], limit=100, mode=mode)
                    modes[mode] = {"jds": [alias(item.revision_id)[:8] for item in rows], "error": None}
                except Exception as error:
                    modes[mode] = {"jds": [], "error": type(error).__name__}
            result["reverse"][person] = modes
            (SNAPSHOT / "match_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("candidate", person, {k: (len(v["jds"]), v["error"]) for k, v in modes.items()}, flush=True)
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
