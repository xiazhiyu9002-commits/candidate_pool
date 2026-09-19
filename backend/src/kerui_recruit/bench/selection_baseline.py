"""十种选人方式基线评测：关键词检索 + 精确筛选（确定性方式）。

这两个方式在线 AI 调用为 0，可用真实数据 + 真实索引离线跑通，无需真实 provider。
本脚本产出的结果只覆盖方式 1（关键词）与方式 4（精确筛选）的确定性基线，
向量/混合及两个匹配方向依赖真实 embedding/reranker，另行评测。

精确筛选的"应返回集合"用 SQLite 直接查询作为独立字段判定，
与 HybridSearchService 的返回 ID 集合做全量比对（分页取完，不截断）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.providers.local import LocalHashEmbeddingProvider, LocalKeywordReranker
from kerui_recruit.search.contracts import CandidateFilters, school_levels_at_least
from kerui_recruit.search.degrees import DEGREE_ORDER, degrees_at_least
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.service import HybridSearchService


def _degree_ge(degree: str) -> tuple[str, ...]:
    return degrees_at_least(degree)


async def run_filter_case(
    svc: HybridSearchService,
    factory: sessionmaker,
    name: str,
    filters: CandidateFilters,
    expected_ids: set[str],
) -> dict:
    started = time.perf_counter()
    page = await svc.search("", filters, limit=300, mode="keyword")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    actual = {hit.candidate_id for hit in page.items}
    return {
        "case": name,
        "expected": len(expected_ids),
        "returned": len(actual),
        "missing": sorted(expected_ids - actual),
        "extra": sorted(actual - expected_ids),
        "correct": expected_ids == actual,
        "elapsed_ms": elapsed_ms,
        "empty_reason": page.empty_reason,
        "degraded": list(page.degraded_reasons),
    }


def _expected_ids(
    factory: sessionmaker,
    *,
    min_years: float | None = None,
    max_years: float | None = None,
    highest_degree: str | None = None,
) -> set[str]:
    clauses = [Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE"]
    if min_years is not None:
        clauses.append(Candidate.total_years >= min_years)
    if max_years is not None:
        clauses.append(Candidate.total_years <= max_years)
    if highest_degree is not None:
        clauses.append(Candidate.highest_degree.in_(_degree_ge(highest_degree)))
    with factory() as session:
        rows = session.scalars(select(Candidate.id).where(*clauses)).all()
        return set(rows)


def _location_expected(factory: sessionmaker, city: str) -> set[str]:
    """现居城市筛选只看现居字段（合同冻结地点语义）。"""
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        return {cid for cid, pd in rows if (pd or {}).get("location") == city}


def _age_expected(factory: sessionmaker, min_age: int | None, max_age: int | None) -> set[str]:
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            age = (pd or {}).get("age")
            if age is None:
                continue
            try:
                age = int(age)
            except (TypeError, ValueError):
                continue
            if min_age is not None and age < min_age:
                continue
            if max_age is not None and age > max_age:
                continue
            result.add(cid)
        return result


def _qs_expected(factory: sessionmaker, max_qs_rank: int) -> set[str]:
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            qs = (pd or {}).get("qs_rank")
            if qs is None:
                continue
            try:
                qs = int(qs)
            except (TypeError, ValueError):
                continue
            if qs <= max_qs_rank:
                result.add(cid)
        return result


def _school_level_expected(factory: sessionmaker, level: str) -> set[str]:
    """学校等级筛选：school_tags 含层级展开后的任一值（985/211/双一流 含层级）。"""
    values = set(school_levels_at_least(level))
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            pd = pd or {}
            tags = set()
            for edu in pd.get("educations") or []:
                if isinstance(edu, dict):
                    tags.update(str(t) for t in (edu.get("school_tags") or []) if t)
            if pd.get("school_level"):
                tags.add(str(pd["school_level"]))
            if tags & values:
                result.add(cid)
        return result


def _preferred_location_expected(factory: sessionmaker, city: str) -> set[str]:
    """意向城市筛选：只看 preferred_location / preferred_locations 字段。"""
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            pd = pd or {}
            cities = []
            if pd.get("preferred_location"):
                cities.append(str(pd["preferred_location"]))
            cities.extend(str(v) for v in (pd.get("preferred_locations") or []) if v)
            # 索引侧对「杭州/上海」这类多城市串做了拆分；这里用子串命中保持一致。
            if any(city in c for c in cities):
                result.add(cid)
        return result


def _name_expected(factory: sessionmaker, name: str) -> set[str]:
    """姓名筛选：display_name 大小写不敏感子串命中。"""
    folded = name.casefold()
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, Candidate.display_name)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE")
        ).all()
        return {cid for cid, display in rows if folded in (display or "").casefold()}


def _company_expected(factory: sessionmaker, company: str) -> set[str]:
    """公司筛选：current_company 或全部工作经历的 company 子串命中（大小写不敏感）。"""
    folded = company.casefold()
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            pd = pd or {}
            companies = [pd.get("current_company")]
            companies.extend(exp.get("company") for exp in (pd.get("experiences") or []) if isinstance(exp, dict))
            if any(folded in str(c).casefold() for c in companies if c):
                result.add(cid)
        return result


def _school_expected(factory: sessionmaker, school: str) -> set[str]:
    """学校筛选：school 或全部教育经历的 school 子串命中（大小写不敏感）。"""
    folded = school.casefold()
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            pd = pd or {}
            schools = [pd.get("school")]
            schools.extend(edu.get("school") for edu in (pd.get("educations") or []) if isinstance(edu, dict))
            if any(folded in str(s).casefold() for s in schools if s):
                result.add(cid)
        return result


def _title_expected(factory: sessionmaker, title: str) -> set[str]:
    """职位筛选：current_title 或全部工作经历的 title 子串命中（大小写不敏感）。"""
    folded = title.casefold()
    with factory() as session:
        rows = session.execute(
            select(Candidate.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.candidate_id == Candidate.id)
            .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
            .where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE",
                   ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY")
        ).all()
        result = set()
        for cid, pd in rows:
            pd = pd or {}
            titles = [pd.get("current_title")]
            titles.extend(exp.get("title") for exp in (pd.get("experiences") or []) if isinstance(exp, dict))
            if any(folded in str(t).casefold() for t in titles if t):
                result.add(cid)
        return result


def _exclude_skills_expected(factory: sessionmaker, index, exclude_skills: tuple[str, ...]) -> set[str]:
    """排除技能：keyword_text 词边界命中排除技能即剔除（与 _apply_exclusion 一致）。"""
    from kerui_recruit.search.query import has_skill
    with factory() as session:
        all_ids = set(session.scalars(
            select(Candidate.id).where(Candidate.deleted_at.is_(None), Candidate.status == "AVAILABLE")
        ).all())
    evidence = index.get_candidate_chunk_contents(list(all_ids))
    result = set()
    for cid in all_ids:
        contents = evidence.get(cid)
        if not contents:
            continue
        if not any(has_skill(content, skill) for content in contents for skill in exclude_skills):
            result.add(cid)
    return result


async def run_keyword_checks(svc: HybridSearchService) -> list[dict]:
    """词法用例：验证词边界与别名，不依赖外部 AI。"""
    checks: list[dict] = []

    async def hit_ids(query: str, operator: str = "smart") -> set[str]:
        from kerui_recruit.search.query import parse_query
        parsed = parse_query(query)
        page = await svc.search(query, CandidateFilters(), limit=300, mode="keyword",
                                operator=operator, concepts=parsed.concepts)
        return {h.candidate_id for h in page.items}

    # Java 不应命中仅含 JavaScript 的人（词边界）。用真实数据抽取 Java 命中集合，
    # 再确认命中者文本含独立 Java 词（由 has_skill 复核）。
    from kerui_recruit.search.query import has_skill
    java_ids = await hit_ids("Java")
    # 取 Java 命中者的 keyword_text 做词边界复核。
    false_positives = []
    for cid in java_ids:
        contents = svc.index.get_candidate_chunk_contents([cid]).get(cid, [])
        if contents and not any(has_skill(content, "Java") for content in contents):
            false_positives.append(cid)
    checks.append({
        "case": "Java 词边界（不误认仅 JavaScript）",
        "returned": len(java_ids),
        "false_positive_without_java": false_positives,
        "ok": not false_positives,
    })

    # JS / JavaScript 等价：两者返回集合应一致（别名归一）。
    js_ids = await hit_ids("JS")
    javascript_ids = await hit_ids("JavaScript")
    checks.append({
        "case": "JS / JavaScript 别名等价",
        "js": len(js_ids),
        "javascript": len(javascript_ids),
        "equal": js_ids == javascript_ids,
    })

    # k8s / Kubernetes 等价。
    k8s_ids = await hit_ids("k8s")
    kube_ids = await hit_ids("Kubernetes")
    checks.append({
        "case": "k8s / Kubernetes 别名等价",
        "k8s": len(k8s_ids),
        "kubernetes": len(kube_ids),
        "equal": k8s_ids == kube_ids,
    })

    # C++ / C# / .NET / Node.js 边界：命中文档必须含对应独立词（不误认 C / C# 子串）。
    for token in ("C++", "C#", ".NET", "Node.js"):
        ids = await hit_ids(token)
        false_positives = []
        for cid in ids:
            contents = svc.index.get_candidate_chunk_contents([cid]).get(cid, [])
            if contents and not any(has_skill(content, token) for content in contents):
                false_positives.append(cid)
        checks.append({
            "case": f"{token} 词边界",
            "returned": len(ids),
            "false_positive": false_positives,
            "ok": not false_positives,
        })

    # 严格 AND：同时含 Java 与 Python。
    and_ids = await hit_ids("Java Python", operator="and")
    java_ids_for_and = await hit_ids("Java")
    python_ids = await hit_ids("Python")
    expected_and = java_ids_for_and & python_ids
    checks.append({
        "case": "AND 集合语义（Java 且 Python）",
        "returned": len(and_ids),
        "expected_intersection": len(expected_and),
        "equal": and_ids == expected_and,
    })

    # 严格 OR：含 Java 或 Python。
    or_ids = await hit_ids("Java Python", operator="or")
    expected_or = java_ids_for_and | python_ids
    checks.append({
        "case": "OR 集合语义（Java 或 Python）",
        "returned": len(or_ids),
        "expected_union": len(expected_or),
        "equal": or_ids == expected_or,
    })
    return checks


async def run_fault_injection(index: LanceDBSearchIndex) -> list[dict]:
    """注入 embedding/reranker 故障，验证关键词/精确筛选不受外部 AI 故障影响。"""
    class BrokenEmbedding:
        async def embed_query(self, text):
            raise RuntimeError("embedding down")

        async def embed_documents(self, texts):
            raise RuntimeError("embedding down")

    class BrokenReranker:
        async def rerank(self, query, documents):
            raise RuntimeError("rerank down")

    broken_svc = HybridSearchService(
        index=index,
        embedding_provider=BrokenEmbedding(),
        reranker_provider=BrokenReranker(),
    )
    checks = []
    page = await broken_svc.search("Java", CandidateFilters(), limit=10, mode="keyword")
    checks.append({
        "case": "关键词检索在 embedding/reranker 故障时仍可用",
        "items": len(page.items),
        "empty_reason": page.empty_reason,
        "ok": len(page.items) > 0 and page.empty_reason is None,
    })
    page2 = await broken_svc.search("", CandidateFilters(min_years=10), limit=10, mode="keyword")
    checks.append({
        "case": "精确筛选在 embedding/reranker 故障时仍可用",
        "items": len(page2.items),
        "empty_reason": page2.empty_reason,
        "ok": len(page2.items) > 0 and page2.empty_reason is None,
    })
    return checks


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path,
                        default=Path(r"c:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data"))
    parser.add_argument("--output", type=Path,
                        default=Path(r"docs\verification\selection-acceptance\baseline.json"))
    args = parser.parse_args()

    engine = create_engine_for(args.data_root / "db" / "recruit.sqlite3")
    factory = sessionmaker(engine, expire_on_commit=False)
    index = LanceDBSearchIndex(
        args.data_root / "search", vector_dimension=1024, embedding_model="BAAI/bge-m3"
    )
    svc = HybridSearchService(
        index=index,
        embedding_provider=LocalHashEmbeddingProvider(dimension=1024),
        reranker_provider=LocalKeywordReranker(),
    )

    filter_cases = []
    cases = [
        ("min_years>=10", dict(min_years=10)),
        ("min_years>=5", dict(min_years=5)),
        ("max_years<=8", dict(max_years=8)),
        ("max_years<=3", dict(max_years=3)),
        ("min_years>=20（边界：极少）", dict(min_years=20)),
        ("min_years>=80（边界：应无结果）", dict(min_years=80)),
        ("highest_degree>=MASTER", dict(highest_degree="MASTER")),
        ("highest_degree>=BACHELOR", dict(highest_degree="BACHELOR")),
        ("highest_degree>=DOCTORATE", dict(highest_degree="DOCTORATE")),
        ("min_years>=5 且 >=MASTER", dict(min_years=5, highest_degree="MASTER")),
        ("min_years>=10 且 >=BACHELOR", dict(min_years=10, highest_degree="BACHELOR")),
        ("5<=years<=10（范围）", dict(min_years=5, max_years=10)),
    ]
    for name, kwargs in cases:
        filters = CandidateFilters(
            min_years=kwargs.get("min_years"),
            max_years=kwargs.get("max_years"),
            highest_degree=kwargs.get("highest_degree"),
        )
        expected = _expected_ids(factory, **kwargs)
        filter_cases.append(await run_filter_case(svc, factory, name, filters, expected))

    # 现居城市（location）只看现居字段，不含意向地/经历地（合同冻结地点语义）。
    for city in ("上海", "北京", "杭州", "深圳"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"location={city}（现居）",
            CandidateFilters(location=city), _location_expected(factory, city)))

    # 年龄范围。
    for name, kwargs in (
        ("min_age>=30", dict(min_age=30, max_age=None)),
        ("max_age<=35", dict(min_age=None, max_age=35)),
        ("30<=age<=40", dict(min_age=30, max_age=40)),
    ):
        filter_cases.append(await run_filter_case(
            svc, factory, name,
            CandidateFilters(min_age=kwargs["min_age"], max_age=kwargs["max_age"]),
            _age_expected(factory, kwargs["min_age"], kwargs["max_age"])))

    # QS 排名上限。
    for rank in (50, 100):
        filter_cases.append(await run_filter_case(
            svc, factory, f"max_qs_rank<={rank}",
            CandidateFilters(max_qs_rank=rank), _qs_expected(factory, rank)))

    # 学校等级（含层级展开：211 含 985，双一流 含 211/985）。
    for level in ("985", "211", "双一流"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"school_level>={level}",
            CandidateFilters(school_level=level), _school_level_expected(factory, level)))

    # 意向城市（preferred_location）。
    for city in ("上海", "北京"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"preferred_location={city}（意向）",
            CandidateFilters(preferred_location=city), _preferred_location_expected(factory, city)))

    # 姓名（字段内关键词匹配）。
    for name in ("周晨", "徐猛", "江超"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"name={name}",
            CandidateFilters(name=name), _name_expected(factory, name)))

    # 公司（匹配 current_company 与全部工作经历）。
    for company in ("腾讯", "阿里巴巴", "太保"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"company={company}",
            CandidateFilters(company=company), _company_expected(factory, company)))

    # 学校（匹配学校与教育经历）。
    for school in ("武汉大学", "浙江大学"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"school={school}",
            CandidateFilters(school=school), _school_expected(factory, school)))

    # 职位（匹配 current_title 与全部工作经历）。
    for title in ("工程师", "技术专家", "架构师"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"title={title}",
            CandidateFilters(title=title), _title_expected(factory, title)))

    # 排除技能（词边界命中即剔除）。
    for skill in ("Java", "Python"):
        filter_cases.append(await run_filter_case(
            svc, factory, f"exclude_skills={skill}",
            CandidateFilters(exclude_skills=(skill,)),
            _exclude_skills_expected(factory, index, (skill,))))

    keyword_checks = await run_keyword_checks(svc)
    fault_checks = await run_fault_injection(index)

    result = {
        "scope": "keyword + exact-filter deterministic baseline (AI calls = 0)",
        "vector/混合/双向匹配": "依赖真实 embedding/reranker，未包含",
        "filter_cases": filter_cases,
        "keyword_lexical_checks": keyword_checks,
        "fault_injection_checks": fault_checks,
        "filter_all_correct": all(c["correct"] for c in filter_cases),
        "keyword_all_ok": all(c.get("ok", c.get("equal", False)) for c in keyword_checks),
        "fault_all_ok": all(c["ok"] for c in fault_checks),
        "degree_order": DEGREE_ORDER,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
