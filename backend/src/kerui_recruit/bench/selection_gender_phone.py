"""gender / phone 精确筛选的正确性验证（走完整 API，含数据库下沉）。

gender / phone 在 API 层通过 `_resolve_structural_candidates` 下沉到 SQLite，
不在 search service 内。这里走真实 FastAPI 路由，与 SQLite 独立字段判定比对。
phone 的独立判定需原始手机号（数据为加密存储），本脚本仅验证 gender。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.search import router
from kerui_recruit.db.models import Candidate, CandidateContact, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.local import LocalHashEmbeddingProvider, LocalKeywordReranker
from kerui_recruit.resumes.normalize import normalize_gender
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


def _gender_expected(factory: sessionmaker, gender: str) -> set[str]:
    target = normalize_gender(gender)
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        return {cid for cid, pd in rows if normalize_gender((pd or {}).get("gender")) == target}


def _phone_expected(factory: sessionmaker, fingerprint: str) -> set[str]:
    with factory() as session:
        rows = session.execute(
            select(CandidateContact.candidate_id)
            .join(Candidate, Candidate.id == CandidateContact.candidate_id)
            .where(CandidateContact.phone_fingerprint == fingerprint,
                   Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE")
        ).all()
        return {cid for (cid,) in rows}


async def measure(data_root: Path) -> dict:
    engine = create_engine_for(data_root / "db" / "recruit.sqlite3")
    factory = sessionmaker(engine, expire_on_commit=False)
    index = LanceDBSearchIndex(
        data_root / "search", vector_dimension=1024, embedding_model="BAAI/bge-m3"
    )
    svc = HybridSearchService(
        index=index,
        embedding_provider=LocalHashEmbeddingProvider(dimension=1024),
        reranker_provider=LocalKeywordReranker(),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(
        session_factory=factory, encryption_service=None, search_service=svc
    )

    cases = []
    phone_cases = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bench"
    ) as client:
        for gender in ("男", "女"):
            expected = _gender_expected(factory, gender)
            resp = await client.post("/api/search/candidates",
                                     json={"query": "", "mode": "keyword", "limit": 100,
                                           "filters": {"gender": gender}})
            body = resp.json()
            actual = {item["candidate_id"] for item in body.get("items", [])}
            cases.append({
                "gender": gender,
                "expected": len(expected),
                "returned": len(actual),
                "missing": sorted(expected - actual),
                "extra": sorted(actual - expected),
                "correct": expected == actual,
            })

        # phone 验证：normalize_phone 幂等（fingerprint 为纯数字），直接用 fingerprint 作查询值。
        with factory() as session:
            fingerprints = session.scalars(
                select(CandidateContact.phone_fingerprint)
                .where(CandidateContact.phone_fingerprint.isnot(None))
                .limit(10)
            ).all()
        for fingerprint in fingerprints:
            expected = _phone_expected(factory, fingerprint)
            resp = await client.post("/api/search/candidates",
                                     json={"query": "", "mode": "keyword", "limit": 100,
                                           "filters": {"phone": fingerprint}})
            body = resp.json()
            actual = {item["candidate_id"] for item in body.get("items", [])}
            phone_cases.append({
                "fingerprint": fingerprint,
                "expected": len(expected),
                "returned": len(actual),
                "missing": sorted(expected - actual),
                "extra": sorted(actual - expected),
                "correct": expected == actual,
            })

    return {
        "gender_cases": cases,
        "gender_all_correct": all(c["correct"] for c in cases),
        "phone_cases": phone_cases,
        "phone_all_correct": all(c["correct"] for c in phone_cases),
        "all_correct": all(c["correct"] for c in cases + phone_cases),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path,
                        default=Path(r"c:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data"))
    parser.add_argument("--output", type=Path,
                        default=Path(r"docs\verification\selection-acceptance\gender.json"))
    args = parser.parse_args()
    result = await measure(args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
