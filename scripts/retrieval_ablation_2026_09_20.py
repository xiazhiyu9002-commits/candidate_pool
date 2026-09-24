"""阶段 3 / 阶段 4 消融：隔离索引 v8 vs v9、分类型召回档位、融合权重、概念覆盖率三种用法。

两个 suite：

- ``--suite index``（阶段 3）：从**同一份** SQLite 快照出发，在隔离目录分别构建 v8 与 v9
  文档的候选人索引（父向量口径 + 子片段前缀/技术栈差异），用同一 embedding 模型跑同一组
  查询，输出两臂的 top-K 别名序列与 Jaccard。**不碰** ``.dev-data/search``。
- ``--suite readside``（阶段 4）：在现役索引上切换读侧档位并跑同一组查询：
  - ``VECTOR_KIND_RECALL_QUOTA``：None（基线，四种 kind 一次取 100）/ 20 / 40 / 60
  - ``FUSION_WEIGHT_BM25`` / ``FUSION_WEIGHT_VECTOR``：1.0/1.0、1.25/1.0、1.0/1.25
  - ``CONCEPT_COVERAGE_MODE``：off / tiebreak / light
  输出每档相对基线的 top-K Jaccard、各 chunk kind 进入融合池/证据包的比例、耗时 P50/P95。

用法：

    py -3.12 scripts/retrieval_ablation_2026_09_20.py --suite readside \
        --queries .tmp-judge/queries.json --output .tmp-ablation/readside.json

指标只到「排序差异 + 覆盖分布 + 延迟」：是否固化某个档位必须由**独立验收集**的
Recall@k / nDCG / P@k 决定，本脚本不做任何自动定档。

安全：只读 ``.dev-data``；不打印密钥；候选人 ID 用 sha256 别名。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

DEV = ROOT / ".dev-data"
MODES = ("vector", "hybrid")
TOP_K = 10
FUSION_WEIGHTS = ((1.0, 1.0), (1.25, 1.0), (1.0, 1.25))
KIND_QUOTAS: tuple[int | None, ...] = (None, 20, 40, 60)
COVERAGE_MODES = ("off", "tiebreak", "light")
# 隔离索引要嵌入现役全量子片段（实测 3 万+ chunk），必须分批——
# SiliconFlowEmbeddingProvider.embed_documents 会把入参全部塞进**一个**请求。
# 实测（scripts 探针，2026-09-20）：batch=32 串行 28 文本/秒；batch=8 × 8 并发 77 文本/秒；
# 而 batch=32 × 6 并发会把请求挂在网关侧（600s 超时都等不到返回）。故用小批 + 高并发。
EMBED_BATCH = 8
EMBED_CONCURRENCY = 8
# 构建隔离索引时的候选人窗口：控制内存峰值（分批嵌入 + 分批落库）。
CANDIDATE_BATCH = 200


async def embed_in_batches(embedding, texts: list[str]) -> list[list[float]]:
    """分批 + 有界并发嵌入，避免单请求塞入数万条文本，也避免串行等待。"""
    batches = [texts[start:start + EMBED_BATCH]
               for start in range(0, len(texts), EMBED_BATCH)]
    vectors: list[list[float]] = []
    for start in range(0, len(batches), EMBED_CONCURRENCY):
        window = batches[start:start + EMBED_CONCURRENCY]
        parts = await asyncio.gather(*(embedding.embed_documents(batch) for batch in window))
        for batch, part in zip(window, parts):
            if len(part) != len(batch):
                raise ValueError(f"embedding 数量不匹配：{len(part)} != {len(batch)}")
            vectors.extend(part)
    return vectors


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_queries(path: Path, phrasings: tuple[str, ...] = ("vague",)) -> list[tuple[str, str]]:
    """读查询集。

    支持 ``{"J01": {"vague": "...", "standard": "...", "colloquial": "..."}}`` 与
    ``[{"query": "..."}]`` 两种形状。字典形状里允许存在 ``_note`` 之类的说明性字符串键，
    因此只在值是 dict 且指定问法非空时才取用——20 个 JD × 3 种问法即文档要求的
    「60–100 条按桶分层」查询集（vague=模糊短查询、colloquial=口语、standard=自然语言职责）。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        queries: list[tuple[str, str]] = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            for phrasing in phrasings:
                text = str(value.get(phrasing) or "").strip()
                if text:
                    queries.append((f"{key}:{phrasing}", text))
        return queries
    return [(str(index).zfill(2), str(item.get("query") or "")) for index, item in enumerate(payload, 1)]


def query_set_fingerprint(path: Path) -> dict:
    """查询集指纹：用于证明「调参集」与「最终验收集」是不同的文件。"""
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()[:16], "bytes": len(raw)}


def frozen_context(queries_path: Path, queries: list[tuple[str, str]]) -> dict:
    """冻结口径：数据库、索引版本、embedding / reranker 模型与查询集身份。

    阶段 0 要求「冻结数据库、索引、embedding 模型、reranker 模型和查询集」，这份记录
    是复现实验的最小充分集：缺任何一项，两次运行的指标都不可比。
    """
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    metadata = json.loads((DEV / "search" / "candidate-index-metadata.json").read_text(encoding="utf-8-sig"))
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    try:
        candidates = connection.execute("SELECT COUNT(*) FROM candidate WHERE deleted_at IS NULL").fetchone()[0]
        jds = connection.execute("SELECT COUNT(*) FROM jd WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        connection.close()
    return {
        "database": {"path": str(DEV / "db" / "recruit.sqlite3"),
                     "candidates": candidates, "jds": jds},
        "index": {"path": str(DEV / "search"), "schema_version": metadata.get("schema_version"),
                  "chunk_version": metadata.get("chunk_version"),
                  "embedding_model": metadata.get("embedding_model"),
                  "vector_dimension": metadata.get("vector_dimension")},
        "embedding_model": settings.get("siliconflow_embedding_model"),
        "reranker_model": settings.get("siliconflow_reranker_model"),
        "queries": {**query_set_fingerprint(queries_path), "count": len(queries)},
    }


def jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if (a | b) else 1.0


def ndcg_at(seq: list[str], grades: dict[str, int], k: int = 10) -> float | None:
    """与 ``judge_eval_metrics_2026_09_19.py`` 同口径：gain = 2^grade − 1，理想序取已判决池降序。"""
    dcg = sum((2 ** grades.get(candidate, 0) - 1) / math.log2(position + 2)
              for position, candidate in enumerate(seq[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2**grade - 1) / math.log2(position + 2) for position, grade in enumerate(ideal))
    return dcg / idcg if idcg > 0 else None


def precision_at(seq: list[str], grades: dict[str, int], k: int = 5) -> float:
    """强相关（grade ≥ 2）占比，与判决式评测口径一致。"""
    if not seq:
        return 0.0
    window = seq[:k]
    return sum(1 for candidate in window if grades.get(candidate, 0) >= 2) / len(window)


def strong_recall_at(seq: list[str], grades: dict[str, int], k: int = 10) -> float | None:
    """该 JD 已判决池里的全部强相关者，被本条前 k 命中的比例（同一 JD 分母一致）。"""
    strong = {candidate for candidate, grade in grades.items() if grade >= 2}
    if not strong:
        return None
    return len(strong & set(seq[:k])) / len(strong)


def load_grades(judge_dir: Path) -> dict[str, dict[str, int]]:
    """还原 (JD → 候选人别名 → grade)。别名与报告里的 sha256 别名同构，可直接对齐。"""
    blinding = json.loads((judge_dir / "blinding_map.json").read_text(encoding="utf-8"))
    judgements: dict[str, dict[str, dict]] = {}
    for path in sorted(judge_dir.glob("judge_out_batch*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for jid, body in payload.items():
            judgements[jid] = {item["label"]: item for item in body["judgements"]}
    grades: dict[str, dict[str, int]] = {}
    for jid, info in blinding.items():
        table = judgements.get(jid, {})
        grades[jid] = {candidate: int(table[letter]["grade"])
                       for letter, candidate in (info.get("letters") or {}).items()
                       if letter in table}
    return grades


def score_arm(top: dict[str, dict[str, list[str]]],
              grades: dict[str, dict[str, int]]) -> tuple[dict, dict]:
    """按「JD:问法|模式」逐条算指标，并给出该臂的均值。"""
    per_query: dict[str, dict[str, float | None]] = {}
    for key, modes in top.items():
        table = grades.get(key.split(":", 1)[0])
        if not table:
            continue
        for mode, seq in modes.items():
            per_query[f"{key}|{mode}"] = {
                "ndcg@10": ndcg_at(seq, table),
                "p@5": precision_at(seq, table),
                "hits@10": strong_recall_at(seq, table),
                "top1_ge2": 1.0 if seq and table.get(seq[0], 0) >= 2 else 0.0,
            }
    summary = _mean_metrics(per_query.values())
    summary["queries"] = len(per_query)
    return summary, per_query


def _mean_metrics(entries) -> dict:
    rows = list(entries)
    def mean(metric: str) -> float | None:
        values = [entry[metric] for entry in rows if entry[metric] is not None]
        return round(statistics.mean(values), 4) if values else None
    return {metric: mean(metric) for metric in ("ndcg@10", "p@5", "hits@10", "top1_ge2")}


def mean_by_mode(per_query: dict[str, dict]) -> dict[str, dict]:
    """把逐条指标按模式再聚合一次。

    读侧报告一次跑多个模式，`score_arm` 的均值会把 keyword/vector/hybrid 混在一起，
    答不了「每个模式各是多少分」，所以额外给一份按模式的分解。
    """
    buckets: dict[str, list[dict]] = {}
    for key, entry in per_query.items():
        buckets.setdefault(key.rsplit("|", 1)[-1], []).append(entry)
    return {mode: _mean_metrics(entries) | {"queries": len(entries)}
            for mode, entries in sorted(buckets.items())}


def compare_arms(scored: dict[str, tuple[dict, dict]], baseline: str) -> dict:
    """相对基线的配对差异：均值差 + 胜负平计数。"""
    base = scored.get(baseline, ({}, {}))[1]
    report: dict[str, dict] = {}
    for name, (_summary, per_query) in scored.items():
        if name == baseline:
            continue
        deltas: dict[str, list[float]] = {}
        wins = losses = ties = 0
        for key, entry in per_query.items():
            against = base.get(key)
            if not against:
                continue
            left, right = entry["ndcg@10"], against["ndcg@10"]
            if left is None or right is None:
                continue
            difference = left - right
            deltas.setdefault("ndcg@10", []).append(difference)
            if difference > 1e-9:
                wins += 1
            elif difference < -1e-9:
                losses += 1
            else:
                ties += 1
        report[name] = {
            "mean_ndcg_delta": round(statistics.mean(deltas["ndcg@10"]), 4) if deltas.get("ndcg@10") else None,
            "wins": wins, "losses": losses, "ties": ties,
        }
    return report


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * ratio) - 1)]


def read_snapshot_rows() -> list[dict]:
    """从 SQLite 读取当前可用修订的 parsed_data（只读，连接只用于 SELECT）。"""
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT r.id AS revision_id, r.parsed_data AS parsed_data,
                   c.id AS candidate_id, c.display_name AS display_name, c.status AS status
            FROM resume_revision r
            JOIN resume_document d ON d.id = r.document_id
            JOIN candidate c ON c.id = d.candidate_id
            WHERE r.is_current = 1 AND r.status = 'READY' AND c.deleted_at IS NULL
              AND c.status = 'AVAILABLE'
            """
        ).fetchall()
    finally:
        connection.close()
    snapshot: list[dict] = []
    for row in rows:
        parsed = json.loads(row["parsed_data"] or "{}")
        snapshot.append({
            "revision_id": row["revision_id"],
            "candidate_id": row["candidate_id"],
            "display_name": row["display_name"],
            "data": parsed,
        })
    return snapshot


def settings_and_keys() -> tuple[dict, object]:
    from kerui_recruit.encryption.service import EncryptionService

    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key"))
    return settings, key


def providers(settings, key, client):
    from kerui_recruit.providers.siliconflow import (
        SiliconFlowEmbeddingProvider,
        SiliconFlowRerankerProvider,
    )

    embedding = SiliconFlowEmbeddingProvider(
        api_key=key.decrypt(settings["siliconflow_api_key"]), client=client,
        base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
    reranker = SiliconFlowRerankerProvider(
        api_key=key.decrypt(settings["siliconflow_api_key"]), client=client,
        base_url=settings["siliconflow_base_url"], model=settings["siliconflow_reranker_model"])
    return embedding, reranker


def school_alias_groups() -> dict[str, tuple[str, ...]] | None:
    """学校标准名/别名快照；与生产 sync.py 用同一来源，保证隔离索引的词法文本等价。

    取不到时返回 None（build_candidate_document 允许缺省），不让消融因辅助数据缺失而失败。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kerui_recruit.schools.reference import SchoolReference

    try:
        engine = create_engine(
            f"sqlite:///file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro&uri=true")
        factory = sessionmaker(engine, expire_on_commit=False)
        groups = SchoolReference(factory).alias_groups()
        engine.dispose()
        return groups
    except Exception as error:  # noqa: BLE001 - 辅助数据缺失不应阻断消融
        print(f"  [warn] 学校别名组不可用（{type(error).__name__}），按空别名继续", flush=True)
        return None


async def build_isolated_index(root: Path, version: str, snapshot: list[dict], embedding) -> int:
    """按指定文档口径在隔离目录构建候选人索引（父 + 子 chunk）。

    按候选人分批「构造 → 嵌入 → 落库」，而不是把 3 万条 1024 维向量全留在内存里：
    全留的内存峰值约 1–2GB，分批后只保留当前窗口。
    """
    from kerui_recruit.search.contracts import SearchChunk
    from kerui_recruit.search.documents import build_candidate_document, build_child_documents
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex

    dimension = live_index_dimension()
    index = LanceDBSearchIndex(root, vector_dimension=dimension, embedding_model="ablation")
    aliases = school_alias_groups()
    total = 0
    windows = (len(snapshot) + CANDIDATE_BATCH - 1) // CANDIDATE_BATCH
    for window_index, start in enumerate(range(0, len(snapshot), CANDIDATE_BATCH), 1):
        window = snapshot[start:start + CANDIDATE_BATCH]
        chunks: list[tuple[dict, dict, int]] = []
        texts: list[str] = []
        for entry in window:
            data = entry["data"]
            doc = build_candidate_document(data, display_name=entry["display_name"],
                                           vector_version=version, school_alias_groups=aliases)
            # 与生产 sync.py 同口径：父文档两段文本都为空时整条候选人跳过，否则会把空
            # 字符串发给 embedding 端点（上游返回不可重试的 E_API_FORMAT，整个构建失败）。
            if not doc["keyword_text"].strip() and not doc["vector_text"].strip():
                continue
            documents = [{"kind": "parent", **doc, "chunk_type": "parent", "parent_id": None}]
            for child in build_child_documents(data, vector_version=version):
                if not child["vector_text"].strip():
                    continue
                # 子片段与生产一致：词法与向量文本都取该片段自身，硬字段继承父。
                documents.append({
                    **doc,
                    "keyword_text": child["vector_text"],
                    "keyword_index_text": child["keyword_index_text"],
                    "vector_text": child["vector_text"],
                    "chunk_type": "child",
                    "parent_id": entry["revision_id"],
                    "kind": child["kind"],
                    "sequence": child.get("sequence"),
                    "evidence_path": child.get("evidence_path") or [],
                })
            for position, document in enumerate(documents):
                texts.append(document["vector_text"])
                chunks.append((entry, document, position))
        vectors = await embed_in_batches(embedding, texts)
        if len(vectors) != len(chunks):
            raise ValueError(f"embedding 数量不匹配：{len(vectors)} != {len(chunks)}")
        payload = [
            SearchChunk(
                id=f"{entry['revision_id']}:{position}",
                candidate_id=entry["candidate_id"],
                revision_id=entry["revision_id"],
                content=document["keyword_text"],
                vector=tuple(vector),
                total_years=document.get("total_years"),
                highest_degree=document.get("highest_degree"),
                location=document.get("location"),
                candidate_status="AVAILABLE",
                keyword_text=document["keyword_text"],
                keyword_index_text=document["keyword_index_text"],
                vector_text=document["vector_text"],
                body_index_text=document.get("body_index_text") or "",
                chunk_type=document.get("chunk_type") or ("parent" if position == 0 else "child"),
                parent_id=document.get("parent_id"),
                kind=document.get("kind") or "parent",
                sequence=document.get("sequence"),
                evidence_path=tuple(document.get("evidence_path") or ()),
                skills=tuple(document.get("skills") or ()),
                career_directions=tuple(document.get("career_directions") or ()),
                career_specializations=tuple(document.get("career_specializations") or ()),
                business_directions=tuple(document.get("business_directions") or ()),
            )
            for (entry, document, position), vector in zip(chunks, vectors)
        ]
        index.upsert(payload)
        total += len(payload)
        print(f"  [{version}] 窗口 {window_index}/{windows}：累计 {total} chunk", flush=True)
    return total


def live_index_dimension() -> int:
    """取现役候选人索引的向量维度，保证隔离索引与 embedding 输出一致。"""
    metadata = json.loads((DEV / "search" / "candidate-index-metadata.json").read_text(encoding="utf-8-sig"))
    return int(metadata["vector_dimension"])


def attach_metrics(results: dict[str, dict], baseline: str, judge_dir: Path | None) -> dict | None:
    """给报告挂上判决式指标（nDCG@10 / P@5 / 强相关召回）；缺标注时返回 None。

    这是「发布门槛」的唯一判据来源：只看 Jaccard 无法判断改口径是变好还是变坏。
    """
    if judge_dir is None or not (judge_dir / "blinding_map.json").exists():
        return None
    grades = load_grades(judge_dir)
    scored = {name: score_arm(payload.get("top") or {}, grades) for name, payload in results.items()}
    return {
        "judge_dir": str(judge_dir),
        "baseline": baseline,
        "arms": {name: summary for name, (summary, _) in scored.items()},
        "arms_by_mode": {name: mean_by_mode(per_query) for name, (_summary, per_query) in scored.items()},
        "vs_baseline": compare_arms(scored, baseline),
        "note": "口径同 judge_eval_metrics_2026_09_19.py；hits@10 即「已判决强相关集被前 10 命中比例」。"
                "本集为调参集，定档仍需独立验收集。",
    }


async def run_suite_index(queries: list[tuple[str, str]], output: Path, modes: tuple[str, ...],
                          frozen: dict, judge_dir: Path | None = None) -> None:
    import shutil

    import httpx

    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.service import HybridSearchService

    settings, key = settings_and_keys()
    snapshot = read_snapshot_rows()
    roots = {"v8": ROOT / ".tmp-ablation/index-v8", "v9": ROOT / ".tmp-ablation/index-v9"}
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=600) as client:
        embedding, reranker = providers(settings, key, client)
        chunk_counts = {}
        for version, root in roots.items():
            shutil.rmtree(root, ignore_errors=True)
            chunk_counts[version] = await build_isolated_index(root, version, snapshot, embedding)
            print(f"构建 {version} 隔离索引：{chunk_counts[version]} chunk", flush=True)
        for version, root in roots.items():
            index = LanceDBSearchIndex(root, vector_dimension=live_index_dimension(),
                                       embedding_model="ablation")
            service = HybridSearchService(index=index, embedding_provider=embedding,
                                          reranker_provider=reranker, search_timeout=600)
            results[version] = await _run_queries(service, queries, modes)
    report = {"frozen": frozen, "chunk_counts": chunk_counts, "results": results,
              "jaccard_v8_vs_v9": _pairwise_jaccard(results)}
    metrics = attach_metrics(results, "v8", judge_dir)
    if metrics is not None:
        report["metrics"] = metrics
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("v8 vs v9 top-K Jaccard：", report["jaccard_v8_vs_v9"])
    if metrics is not None:
        print("判决式指标：", json.dumps(metrics, ensure_ascii=False))
    print(f"产物：{output}")


async def run_suite_readside(queries: list[tuple[str, str]], output: Path, modes: tuple[str, ...],
                             frozen: dict, judge_dir: Path | None = None,
                             only: tuple[str, ...] | None = None) -> None:
    import httpx

    from kerui_recruit.search import lancedb_index as index_module
    from kerui_recruit.search import service as service_module
    from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
    from kerui_recruit.search.observer import InMemorySearchObserver
    from kerui_recruit.search.service import HybridSearchService

    settings, key = settings_and_keys()
    index = LanceDBSearchIndex(DEV / "search", vector_dimension=live_index_dimension(),
                               embedding_model=settings["siliconflow_embedding_model"])
    if not index.is_ready():
        raise SystemExit(f"索引不可读：{index.read_compatibility_error}")

    configs: list[tuple[str, dict]] = [("baseline", {"quota": None, "weights": (1.0, 1.0), "coverage": "off"})]
    configs += [(f"quota{quota}", {"quota": quota, "weights": (1.0, 1.0), "coverage": "off"})
                for quota in KIND_QUOTAS if quota]
    configs += [(f"fts{fts}/vec{vec}", {"quota": None, "weights": (fts, vec), "coverage": "off"})
                for fts, vec in FUSION_WEIGHTS if (fts, vec) != (1.0, 1.0)]
    configs += [(f"coverage_{mode}", {"quota": None, "weights": (1.0, 1.0), "coverage": mode})
                for mode in COVERAGE_MODES if mode != "off"]
    if only:
        # keyword 模式只走 FTS，与档位无关；补采该模式时用 --configs baseline 避免重复跑 8 遍。
        unknown = sorted(set(only) - {name for name, _ in configs})
        if unknown:
            raise SystemExit(f"未知档位：{', '.join(unknown)}")
        configs = [item for item in configs if item[0] in set(only)]

    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=600) as client:
        embedding, reranker = providers(settings, key, client)
        service = HybridSearchService(index=index, embedding_provider=embedding,
                                      reranker_provider=reranker, search_timeout=600)
        for name, config in configs:
            service_module.VECTOR_KIND_RECALL_QUOTA = config["quota"]
            index_module.FUSION_WEIGHT_BM25, index_module.FUSION_WEIGHT_VECTOR = config["weights"]
            service_module.CONCEPT_COVERAGE_MODE = config["coverage"]
            observer = InMemorySearchObserver()
            started = time.monotonic()
            results[name] = await _run_queries(service, queries, modes, observer=observer)
            print(f"[{name}] {time.monotonic() - started:.0f}s", flush=True)
            results[name]["kind_distribution"] = _aggregate_kinds(observer)
    # 恢复默认档位，避免影响同进程内的后续调用。
    service_module.VECTOR_KIND_RECALL_QUOTA = None
    index_module.FUSION_WEIGHT_BM25 = index_module.FUSION_WEIGHT_VECTOR = 1.0
    service_module.CONCEPT_COVERAGE_MODE = "off"

    report = {"frozen": frozen, "configs": [name for name, _ in configs],
              "results": results, "jaccard_vs_baseline": _pairwise_jaccard(results, baseline="baseline")}
    metrics = attach_metrics(results, "baseline", judge_dir)
    if metrics is not None:
        report["metrics"] = metrics
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("相对 baseline 的 top-K Jaccard：", report["jaccard_vs_baseline"])
    if metrics is not None:
        print("判决式指标：", json.dumps(metrics, ensure_ascii=False))
    print(f"产物：{output}")


async def _run_queries(service, queries, modes, observer=None) -> dict:
    from kerui_recruit.search.contracts import CandidateFilters

    results: dict = {"top": {}, "latency_ms": []}
    for key, text in queries:
        if not text.strip():
            continue
        results["top"][key] = {}
        for mode in modes:
            started = time.monotonic()
            page = await service.search(text, CandidateFilters(), limit=TOP_K, mode=mode, observer=observer)
            results["latency_ms"].append(round((time.monotonic() - started) * 1000, 1))
            results["top"][key][mode] = [alias(hit.candidate_id) for hit in page.items[:TOP_K]]
    latencies = results["latency_ms"]
    results["latency_summary"] = {"p50": statistics.median(latencies) if latencies else 0.0,
                                  "p95": percentile(latencies, 0.95)}
    return results


def _aggregate_kinds(observer) -> dict[str, int]:
    counts: dict[str, int] = {}
    for payload in observer.diagnostics:
        for kind, value in (payload.get("chunk_kinds") or {}).items():
            counts[kind] = counts.get(kind, 0) + value
    return dict(sorted(counts.items()))


def _pairwise_jaccard(results: dict, baseline: str | None = None) -> dict:
    reference = baseline or "v8"
    base = results.get(reference, {})
    jaccards: dict[str, list[float]] = {}
    for name, payload in results.items():
        if name == reference:
            continue
        values: list[float] = []
        for key, modes in (payload.get("top") or {}).items():
            for mode, ids in modes.items():
                against = ((base.get("top") or {}).get(key) or {}).get(mode)
                if against is not None:
                    values.append(jaccard(ids, against))
        jaccards[name] = [round(statistics.mean(values), 4), len(values)] if values else []
    return jaccards


async def main() -> None:
    parser = argparse.ArgumentParser(description="检索消融：v8/v9 隔离索引与读侧档位")
    parser.add_argument("--suite", choices=("index", "readside"), default="readside")
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--phrasings", default="vague",
                        help="字典形状查询集取哪些问法，逗号分隔；vague,standard,colloquial 即 60 条分层集")
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp-ablation/report.json")
    parser.add_argument("--judge-dir", type=Path, default=ROOT / ".tmp-judge",
                        help="已判决标注目录（blinding_map.json + judge_out_batch*.json）；缺失则只输出排序差异")
    parser.add_argument("--modes", default=",".join(MODES),
                        help="逗号分隔；可加 keyword（纯 FTS，不调用 embedding/reranker）")
    parser.add_argument("--configs", default="",
                        help="只跑指定读侧档位（逗号分隔，默认全部）；补采 keyword 等档位无关的模式用 baseline")
    options = parser.parse_args()

    phrasings = tuple(part.strip() for part in options.phrasings.split(",") if part.strip())
    queries = load_queries(options.queries, phrasings)
    modes = tuple(part for part in options.modes.split(",") if part)
    frozen = frozen_context(options.queries, queries)
    print("冻结口径：", json.dumps(frozen, ensure_ascii=False), flush=True)
    if options.suite == "index":
        await run_suite_index(queries, options.output, modes, frozen, options.judge_dir)
    else:
        only = tuple(part.strip() for part in options.configs.split(",") if part.strip()) or None
        await run_suite_readside(queries, options.output, modes, frozen, options.judge_dir, only)


if __name__ == "__main__":
    asyncio.run(main())
