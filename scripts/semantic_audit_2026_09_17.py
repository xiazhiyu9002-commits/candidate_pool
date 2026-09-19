"""Reproduce the frozen, read-only search retrieval for the semantic audit.

The output contains pseudonymous IDs only. The SQLite and LanceDB inputs are
copied snapshots; this script never writes to either source.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx

from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.providers.siliconflow import (
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.search.contracts import CandidateFilters
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.query import parse_query
from kerui_recruit.search.service import HybridSearchService


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# Fixed before inspecting any retrieval results. All text is generic job intent.
INTENTS = (
    ("Q01", "Java Spring Boot 微服务"),
    ("Q02", "高并发交易系统 Java 支付"),
    ("Q03", "银行核心系统 Avaloq PL/SQL"),
    ("Q04", "Python Agent LangGraph 工具调用"),
    ("Q05", "企业知识库 RAG 检索增强"),
    ("Q06", "多智能体 视频 Agent 工作流"),
    ("Q07", "LLM 模型训练 微调 PyTorch"),
    ("Q08", "推荐算法 召回 排序"),
    ("Q09", "数据仓库 建模 ETL Spark"),
    ("Q10", "实时数据处理 Flink Kafka"),
    ("Q11", "交易主题数仓 数据治理"),
    ("Q12", "BI 指标体系 经营分析"),
    ("Q13", "React TypeScript 前端工程化"),
    ("Q14", "Kubernetes DevOps 可观测性"),
    ("Q15", "B端 产品经理 供应链 WMS"),
    ("Q16", "金融业务分析 产品经理"),
    ("Q17", "技术团队管理 Java 架构"),
    ("Q18", "Java React 全栈"),
    ("Q19", "数据平台后端 Kafka Spark"),
    ("Q20", "Python 算法工程师 RAG"),
    ("Q21", "北京大学 后端工程师"),
    ("Q22", "现居北京 Java 不要 Python"),
    ("Q23", "本科 5年以上 Spring Boot"),
    ("Q24", "SQL Excel 数据分析"),
)


def exact_intents(connection: sqlite3.Connection) -> tuple[tuple[str, str, CandidateFilters], ...]:
    current = """FROM candidate c JOIN resume_document d ON d.candidate_id=c.id
        JOIN resume_revision r ON r.document_id=d.id
        WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL
        AND r.is_current=1 AND r.status='READY'"""
    row = connection.execute(
        "SELECT c.display_name FROM candidate c JOIN resume_document d ON d.candidate_id=c.id "
        "JOIN resume_revision r ON r.document_id=d.id "
        "WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL AND r.is_current=1 AND r.status='READY' "
        "AND length(c.display_name) BETWEEN 2 AND 4 ORDER BY c.id LIMIT 1"
    ).fetchone()
    name = row[0] if row else "无匹配姓名"
    company = None
    for (raw,) in connection.execute("SELECT r.parsed_data " + current + " ORDER BY c.id"):
        parsed = json.loads(raw) if isinstance(raw, str) else raw or {}
        value = parsed.get("current_company")
        if isinstance(value, str) and len(value) >= 3:
            company = value
            break
    return (
        ("Q25", "姓名字段精确筛选（固定快照首个合格姓名）", CandidateFilters(name=name)),
        ("Q26", "学校字段精确筛选（北京大学）", CandidateFilters(school="北京大学")),
        ("Q27", "公司字段精确筛选（固定快照首个合格公司）", CandidateFilters(company=company or "无匹配公司")),
    )


async def main() -> None:
    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    index = LanceDBSearchIndex(SNAPSHOT / "search", vector_dimension=1024, embedding_model=settings["siliconflow_embedding_model"])
    async with httpx.AsyncClient(timeout=40) as client:
        embedding = SiliconFlowEmbeddingProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        reranker = SiliconFlowRerankerProvider(api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])
        service = HybridSearchService(index=index, embedding_provider=embedding,
            reranker_provider=reranker, search_timeout=8)
        results: dict = {
            "snapshot": {"index_rows": index.warmup(), "index_metadata": index.metadata},
            "models": {"embedding": embedding.model, "reranker": reranker.model},
            "intents": {},
        }
        for qid, text in INTENTS:
            parsed = parse_query(text)
            modes = {}
            for mode in ("keyword", "vector", "hybrid"):
                page = await service.search(parsed.keywords, parsed.filters, limit=100,
                    mode=mode, concepts=parsed.concepts)
                modes[mode] = {
                    "ids": [alias(hit.candidate_id) for hit in page.items],
                    "revisions": [alias(hit.revision_id) for hit in page.items],
                    "degraded": list(page.degraded_reasons),
                    "empty_reason": page.empty_reason,
                }
            results["intents"][qid] = {
                "intent": text, "retained_keywords": parsed.keywords,
                "conditions": [asdict(condition) for condition in parsed.conditions],
                "modes": modes,
            }
            print(qid, {mode: (len(value["ids"]), value["degraded"]) for mode, value in modes.items()}, flush=True)
            (SNAPSHOT / "retrieval.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        for qid, label, filters in exact_intents(connection):
            page = await service.search("", filters, limit=100, mode="hybrid")
            results["intents"][qid] = {
                "intent": label, "retained_keywords": "", "conditions": [],
                "modes": {"exact": {"ids": [alias(hit.candidate_id) for hit in page.items],
                    "revisions": [alias(hit.revision_id) for hit in page.items],
                    "degraded": list(page.degraded_reasons), "empty_reason": page.empty_reason}},
            }
            print(qid, len(page.items), page.degraded_reasons, flush=True)
        (SNAPSHOT / "retrieval.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.close()


if __name__ == "__main__":
    asyncio.run(main())
