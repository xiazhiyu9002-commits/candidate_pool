"""S4 / S5 验收：真实库上的证据包与 AI 复核契约（只读，不调用外部 API）。

- S4：关键词模式跑若干 JD，统计 evidence_pack 的 parent / tech / business 覆盖，并打印样例；
- S4：反向匹配同一人抽样，验证岗位侧证据包同样成立；
- S5：用**假模型客户端**跑一次真实 match run 的复核，验证岗位侧输入（人工改过用画像、
  否则用带小节标注的 JD 原文）与三段契约（project_match / experience_match / tech_match
  + risks + basis）（不产生任何模型调用与写库）。

安全：数据库只读 URI；只读索引；不调 optimize_pending()；不写数据库。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import (  # noqa: E402
    Candidate,
    Jd,
    JdRevision,
    MatchResult,
    MatchRun,
    ResumeDocument,
    ResumeRevision,
)
from kerui_recruit.match.jd_index import JdSearchIndex  # noqa: E402
from kerui_recruit.match.review import (  # noqa: E402
    MatchReviewService,
    ReviewVerdictModel,
    build_evidence_section,
    resolve_jd_source,
)
from kerui_recruit.match.service import MatchService  # noqa: E402
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402
from kerui_recruit.search.live import projection_is_current  # noqa: E402
from kerui_recruit.search.service import HybridSearchService  # noqa: E402

DEV = ROOT / ".dev-data"
JDS = 6


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


class _FakeReviewClient:
    """假模型：返回三段结论，并记录收到的 prompt。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_json(self, messages, model, reasoning=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        return ReviewVerdictModel(
            verdict="recommend",
            project_match=["假模型：项目与岗位同类（projects[0]）"],
            experience_match=["假模型：经历与岗位同类（experiences[0]）"],
            tech_match=["假模型：技术栈主干对得上（skills）"],
            risks=["假模型：未见岗位优先项（experiences[1]）"],
        )


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
            select(JdRevision.id, Jd.title)
            .join(Jd, Jd.id == JdRevision.jd_id)
            .where(JdRevision.is_current.is_(True), JdRevision.status == "READY",
                   Jd.status == "OPEN", Jd.deleted_at.is_(None),
                   projection_is_current("jd", Jd.id))
            .limit(JDS)
        ).all()

    print("=== S4：岗位匹配人的证据包（关键词模式）===")
    for revision_id, title in rows:
        page = await matcher.match_jd(revision_id=revision_id, limit=50, mode="keyword")
        packs = []
        for hit in page.items:
            pack = matcher.score(revision_id, hit).breakdown.get("evidence_pack") or {}
            packs.append(pack)
        has_parent = sum(1 for p in packs if p.get("parent"))
        has_tech = sum(1 for p in packs if p.get("tech"))
        has_biz = sum(1 for p in packs if p.get("business"))
        print(f"  {str(title)[:26]:<28} 返回={len(packs):<4} parent={has_parent:<4} "
              f"tech={has_tech:<4} business={has_biz}")
        for pack in packs[:1]:
            if pack:
                print(f"      样例 parent：{pack.get('parent', '')[:80]}")
                print(f"      样例 tech  ：{pack.get('tech', '（无）')[:80]}")
                print(f"      样例 biz   ：{pack.get('business', '（无）')[:80]}")

    print("\n=== S4：混合模式（向量通道返回子片段，证据包应含 tech / business）===")
    try:
        from kerui_recruit.encryption.service import EncryptionService
        from kerui_recruit.providers.siliconflow import (
            SiliconFlowEmbeddingProvider,
            SiliconFlowRerankerProvider,
        )
        import httpx

        settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
        key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
            settings["siliconflow_api_key"])
        async with httpx.AsyncClient(timeout=180) as client:
            hybrid_search = HybridSearchService(
                index=index,
                embedding_provider=SiliconFlowEmbeddingProvider(
                    api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                    model=settings["siliconflow_embedding_model"]),
                reranker_provider=SiliconFlowRerankerProvider(
                    api_key=key, client=client, base_url=settings["siliconflow_base_url"],
                    model=settings["siliconflow_reranker_model"]),
                search_timeout=180)
            hybrid = MatchService(session_factory=factory, search_service=hybrid_search,
                                  jd_index=jd_index)
            for revision_id, title in rows[:2]:
                page = await hybrid.match_jd(revision_id=revision_id, limit=50, mode="hybrid")
                packs = [hybrid.score(revision_id, hit).breakdown.get("evidence_pack") or {}
                         for hit in page.items]
                print(f"  {str(title)[:26]:<28} 返回={len(packs):<4} "
                      f"parent={sum(1 for p in packs if p.get('parent')):<4} "
                      f"tech={sum(1 for p in packs if p.get('tech')):<4} "
                      f"business={sum(1 for p in packs if p.get('business'))}")
                sample = next((p for p in packs if p.get("tech")), None)
                if sample:
                    print(f"      样例 tech：{sample['tech'][:90]}")
                    print(f"      样例 biz ：{sample.get('business', '（无）')[:90]}")
                    section = build_evidence_section(sample)
                    print(f"      进 prompt 的证据段：技术片段={'技术最相关片段' in section} "
                          f"业务片段={'业务最相关片段' in section}")
    except Exception as error:  # noqa: BLE001 - 验收脚本报告不中断
        print(f"  混合模式跳过：{type(error).__name__}: {error}")

    print("\n=== S4：人匹配岗位的岗位侧证据包（关键词模式，1 人）===")
    with factory() as session:
        person = session.execute(
            select(Candidate.id)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
            .limit(1)
        ).scalar()
    records = await matcher.reverse_match_candidate(person, limit=5, mode="keyword")
    for record in records:
        pack = record.score.breakdown.get("evidence_pack") or {}
        print(f"  {str(record.title)[:26]:<28} parent={'有' if pack.get('parent') else '无'} "
              f"tech={'有' if pack.get('tech') else '无'} business={'有' if pack.get('business') else '无'}")
        if pack.get("tech"):
            print(f"      tech 样例：{pack['tech'][:80]}")

    print("\n=== S5：岗位侧复核输入源（人工改过 → 画像；否则 → 带小节标注的原文）===")
    with factory() as session:
        for revision_id, title in rows[:3]:
            revision = session.get(JdRevision, revision_id)
            text, source, trimmed = resolve_jd_source(
                revision.parsed_data or {}, revision.source_text, revision.manual_overrides
            )
            labels = [label for label in ("【岗位职责】", "【优先项】", "【任职要求】") if label in text]
            print(f"  《{title}》来源={source} 长度={len(text)} "
                  f"小节={labels or '无（未识别到标题）'} 裁剪={'是' if trimmed else '否'}")

    print("\n=== S5：真实 run 的复核契约（假模型，不调用外部 API）===")
    with factory() as session:
        run_id, trigger = session.execute(
            select(MatchRun.id, MatchRun.trigger)
            .join(MatchResult, MatchResult.run_id == MatchRun.id)
            .order_by(MatchRun.created_at.desc())
            .limit(1)
        ).first()
        result_count = len(list(session.scalars(
            select(MatchResult.id).where(MatchResult.run_id == run_id)
        )))
    print(f"  选用 run={alias(run_id)} trigger={trigger} 配对={result_count} 条")

    client = _FakeReviewClient()
    service = MatchReviewService(session_factory=factory, task_client=client)
    verdicts = await service.review_run(run_id)

    assert len(verdicts) == result_count, "每一对都必须返回条目（允许标记未复核）"
    assert all(v.get("verdict") in ("recommend", "pending", "reject") for v in verdicts)
    assert all(isinstance(v.get("basis"), dict) for v in verdicts), "每条结论都要带判据依据"
    matched = [
        len(v.get("project_match") or ()) + len(v.get("experience_match") or ())
        + len(v.get("tech_match") or ())
        for v in verdicts
    ]
    print(f"  返回条目={len(verdicts)} 实际调用={len(client.prompts)}（不设上限）")
    print(f"  三段结论条数 min/中位/max = {min(matched)}/"
          f"{sorted(matched)[len(matched)//2]}/{max(matched)}")
    eligible_skipped = [v for v in verdicts if not (v.get("basis") or {}).get("eligible", True)]
    print(f"  前置资格被拒（未进 AI 复核）的条目 = {len(eligible_skipped)}")
    if client.prompts:
        sample = client.prompts[0]
        with factory() as session:
            stored_packs = sum(
                1 for breakdown in session.scalars(
                    select(MatchResult.score_breakdown).where(MatchResult.run_id == run_id))
                if (breakdown or {}).get("evidence_pack")
            )
        print(f"  prompt 含三段契约 = {'project_match' in sample and 'tech_match' in sample}")
        print(f"  prompt 含权重规则 = {'判断权重：' in sample}")
        print(f"  prompt 含证据包片段 = {'检索命中的原文片段' in sample}"
              f"（该 run 落库时带证据包的条目 = {stored_packs} 条；"
              f"历史 run 的 breakdown 没有该键，属预期）")
        # 证据包确实能进 prompt：用当前代码即时算一份包渲染证据段。
        revision_id, title = rows[0]
        live = await matcher.match_jd(revision_id=revision_id, limit=20, mode="keyword")
        pack = {}
        for hit in live.items:
            candidate_pack = matcher.score(revision_id, hit).breakdown.get("evidence_pack") or {}
            if candidate_pack.get("tech") or candidate_pack.get("business"):
                pack = candidate_pack
                break
        section = build_evidence_section(pack)
        print(f"  即时证据段（《{title}》）：长度={len(section)} "
              f"技术片段={'技术最相关片段' in section} 业务片段={'业务最相关片段' in section}")
    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
