"""检查索引里的向量与文本是否符合设计（**只读**）。

校验四件事：
1. 口径与物理：metadata 与代码常量是否一致、列是否齐、FTS 索引是否在、向量维度/范数是否正常。
2. 分块结构：parent / child 的构成、每候选人 chunk 数、``kind`` 分布、新增字段填充率。
3. ``vector_text`` 构造符合性：父向量是否 = 画像 + 教育 + 城市 + 年限 + 最近公司/职位 + 技能；
   子向量是否 = 「画像浓缩前缀 + 本片段」，且前缀取自 ``ai_profile_compact``。
4. 向量与文本的对应性：相同 ``vector_text`` 是否得到同一向量（缓存键的前提）；
   用 chunk 自身文本反查，能否在候选人层排第一、在该候选人内部拿到最高分。

安全：只读直连 ``.dev-data``，绝不调用 ``optimize_pending()``；不打印密钥；ID 用 sha256 别名。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import httpx  # noqa: E402
import lancedb  # noqa: E402

from kerui_recruit.encryption.service import EncryptionService  # noqa: E402
from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider  # noqa: E402
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import (  # noqa: E402
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)

DEV = ROOT / ".dev-data"
SELF_RETRIEVAL_SAMPLES = 24
VECTOR_SAMPLES = 400


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def norm(vector) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in vector))


def main() -> None:
    db_dir = DEV / "search"
    db = lancedb.connect(str(db_dir))
    table = db.open_table("candidate_chunks")
    total_rows = table.count_rows()

    print("=" * 78)
    print("1. 口径与物理")
    print("=" * 78)
    meta = json.loads((db_dir / "candidate-index-metadata.json").read_text(encoding="utf-8-sig"))
    print(f"metadata          : {json.dumps(meta, sort_keys=True)}")
    print(f"代码期望           : schema={INDEX_SCHEMA_VERSION} chunk={INDEX_CHUNK_VERSION}")
    print(f"schema 一致        : {meta.get('schema_version') == INDEX_SCHEMA_VERSION}")
    print(f"chunk 一致         : {meta.get('chunk_version') == INDEX_CHUNK_VERSION}")
    print(f"表行数             : {total_rows}")
    schema_names = table.schema.names
    for column in ("id", "candidate_id", "chunk_type", "kind", "specializations", "vector"):
        print(f"  列 {column:<16}: {'有' if column in schema_names else '缺'}")
    fts = sorted({getattr(i, "name", "") or "" for i in table.list_indices()})
    print(f"FTS 索引           : {fts}")

    print()
    print("=" * 78)
    print("2. 分块结构")
    print("=" * 78)
    structure = table.search(None).select(
        ["candidate_id", "chunk_type", "kind", "specializations", "sequence", "evidence_path"]
    ).limit(None).to_arrow().to_pylist()
    print(f"chunk_type 分布    : {dict(Counter(r['chunk_type'] for r in structure))}")
    print(f"kind 分布          : {dict(Counter(r.get('kind') or '(空)' for r in structure))}")

    per_candidate: Counter[str] = Counter()
    parents: Counter[str] = Counter()
    for row in structure:
        per_candidate[row["candidate_id"]] += 1
        if row["chunk_type"] == "parent":
            parents[row["candidate_id"]] += 1
    counts = sorted(per_candidate.values())
    print(f"候选人数           : {len(per_candidate)}")
    if counts:
        print(f"每人 chunk 数       : min={counts[0]} 中位={counts[len(counts) // 2]} max={counts[-1]}")
    parents_per = Counter(parents.values())
    print(f"每人 parent 数分布  : {dict(parents_per)}")

    def filled(key: str, predicate) -> str:
        hit = sum(1 for r in structure if predicate(r.get(key)))
        return f"{hit}/{len(structure)} ({hit / len(structure) * 100:.1f}%)"

    print(f"specializations 非空: {filled('specializations', lambda v: bool(v))}")
    print(f"sequence 非空       : {filled('sequence', lambda v: v is not None)}")
    print(f"evidence_path 非空  : {filled('evidence_path', lambda v: bool(v))}")

    print()
    print("=" * 78)
    print("3. vector_text 构造抽样")
    print("=" * 78)
    samples = table.search(None).select(
        ["id", "candidate_id", "revision_id", "chunk_type", "kind", "vector_text", "keyword_index_text"]
    ).limit(3000).to_arrow().to_pylist()
    by_kind: dict[str, list[dict]] = {}
    for row in samples:
        by_kind.setdefault(row.get("kind") or row["chunk_type"], []).append(row)
    for kind, rows in sorted(by_kind.items()):
        row = rows[0]
        text = (row.get("vector_text") or "").strip()
        keyword = (row.get("keyword_index_text") or "").strip()
        print(f"[{kind}] n={len(rows)} len={len(text)} type={row['chunk_type']}")
        print(f"  vector_text        : {text[:150]}")
        print(f"  keyword_index_text : {keyword[:100]}")
        if row["chunk_type"] == "child":
            print(f"  子块 keyword 是否去掉画像前缀: {not keyword.startswith(text[:12])}")
        print()

    print("=" * 78)
    print("4. 子块前缀是否取自 ai_profile_compact")
    print("=" * 78)
    conn = sqlite3.connect("file:" + (DEV / "db" / "recruit.sqlite3").as_posix() + "?mode=ro", uri=True)
    # 必须按「修订」对齐：一个候选人可以有多份 current READY 简历，各修订的画像不同，
    # 只按 candidate_id 取一份会把另一个修订的 compact 拿来比对，造出假未命中。
    compact_by_revision: dict[str, str] = {}
    for revision_id, raw in conn.execute(
        "select r.id, r.parsed_data from resume_revision r "
        "join resume_document d on d.id = r.document_id "
        "where r.is_current = 1 and r.status = 'READY'"
    ):
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("ai_profile_compact"):
            compact_by_revision[revision_id] = " ".join(str(data["ai_profile_compact"]).split())
    conn.close()

    checked = hit = fallback = 0
    misses: list[tuple[str, str, str]] = []
    for row in samples:
        if row["chunk_type"] != "child":
            continue
        compact = compact_by_revision.get(row["revision_id"])
        if not compact:
            fallback += 1
            continue
        checked += 1
        text = " ".join((row.get("vector_text") or "").split())
        if text.startswith(compact):
            hit += 1
        elif len(misses) < 3:
            misses.append((row["kind"] or "", compact[:60], text[:60]))
    print(f"有 compact 的修订数      : {len(compact_by_revision)}")
    print(f"子块命中 compact 前缀    : {hit}/{checked} "
          f"({hit / checked * 100:.1f}%)" if checked else "  无可核对样本")
    print(f"该修订无 compact（走回退）: {fallback}")
    for kind, expected, actual in misses:
        print(f"  未命中 [{kind}] 期望前缀={expected!r}")
        print(f"         实际开头={actual!r}")

    print()
    print("=" * 78)
    print("5. 向量统计与缓存一致性")
    print("=" * 78)
    vectors = table.search(None).select(
        ["id", "candidate_id", "chunk_type", "vector_text", "vector"]
    ).limit(VECTOR_SAMPLES).to_arrow().to_pylist()
    dims = Counter(len(r["vector"]) for r in vectors)
    norms = [norm(r["vector"]) for r in vectors]
    zeros = sum(1 for n in norms if n == 0.0)
    nans = sum(1 for r in vectors for x in r["vector"] if x != x)
    print(f"抽样               : {len(vectors)} 条")
    print(f"维度分布           : {dict(dims)}")
    print(f"L2 范数            : min={min(norms):.4f} 中位={sorted(norms)[len(norms) // 2]:.4f} max={max(norms):.4f}")
    print(f"是否归一化         : {all(abs(n - 1.0) < 0.01 for n in norms)}")
    print(f"零向量={zeros} NaN={nans}")

    by_text: dict[str, set[str]] = {}
    for row in vectors:
        by_text.setdefault(" ".join((row.get("vector_text") or "").split()), set()).add(
            hashlib.sha256(bytes(str(list(row["vector"])), "utf-8")).hexdigest()[:12])
    duplicated = {t: h for t, h in by_text.items() if len(h) > 1 and t}
    print(f"相同 vector_text 的不同向量数 >1 的文本: {len(duplicated)}（应为 0）")

    print()
    print("=" * 78)
    print(f"6. 自检索（{SELF_RETRIEVAL_SAMPLES} 条子块文本反查）")
    print("=" * 78)
    children = [r for r in samples if r["chunk_type"] == "child" and (r.get("vector_text") or "").strip()]
    step = max(1, len(children) // SELF_RETRIEVAL_SAMPLES)
    picked = children[::step][:SELF_RETRIEVAL_SAMPLES]
    asyncio.run(self_retrieval(picked, db_dir, meta))


async def self_retrieval(picked: list[dict], db_dir: Path, meta: dict) -> None:
    settings = json.loads((DEV / "config" / "settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(DEV / "config" / "encryption.key")).decrypt(
        settings["siliconflow_api_key"])
    index = LanceDBSearchIndex(db_dir, vector_dimension=meta["vector_dimension"],
                               embedding_model=meta["embedding_model"])
    print(f"索引 is_ready={index.is_ready()} is_compatible={index.is_compatible()}")

    texts = [" ".join((r.get("vector_text") or "").split()) for r in picked]
    top1 = top5 = intra = 0
    ranking: list[int] = []
    async with httpx.AsyncClient(timeout=60) as client:
        provider = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"],
            model=settings["siliconflow_embedding_model"])
        response = await provider.embed_documents(texts)
        for row, text, vector in zip(picked, texts, response):
            hits = index.search_vector(tuple(vector), CandidateFilters(), limit=5)
            ids = [h["candidate_id"] for h in hits]
            if ids and ids[0] == row["candidate_id"]:
                top1 += 1
            if row["candidate_id"] in ids:
                top5 += 1
                ranking.append(ids.index(row["candidate_id"]) + 1)
            # 候选人内部：目标 chunk 是否拿到最高相似度
            own = index.get_revision_chunks(row["revision_id"])
            if own:
                query_norm = norm(vector) or 1.0
                scored = sorted(
                    ((sum(float(a) * float(b) for a, b in zip(vector, o["vector"]))
                      / (query_norm * (norm(o["vector"]) or 1.0)), o["id"])
                     for o in own if o.get("vector")), reverse=True)
                if scored and scored[0][1] == row["id"]:
                    intra += 1
    total = len(picked)
    print(f"目标候选人排名第 1 : {top1}/{total} ({top1 / total * 100:.1f}%)")
    print(f"目标候选人进前 5   : {top5}/{total} ({top5 / total * 100:.1f}%)")
    print(f"排名分布           : {dict(sorted(Counter(ranking).items()))}")
    print(f"候选人内部目标 chunk 得分最高: {intra}/{total} ({intra / total * 100:.1f}%)")
    print()
    print("抽样明细（别名）：")
    for row in picked[:6]:
        print(f"  [{row.get('kind')}] cand={alias(row['candidate_id'])} "
              f"text={(row.get('vector_text') or '')[:70]}")


if __name__ == "__main__":
    main()
