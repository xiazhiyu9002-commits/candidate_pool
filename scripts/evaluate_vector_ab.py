"""A/B 向量文本消融：对比「去除噪声字段」的变体 B 相对当前 A 的纯向量 P@5。

变体 B = build_candidate_document_b：父向量去掉学校/城市/年限/姓名，仅保留
画像 + 最近公司/职位 + 技能；子 chunk 不变。只重建父 chunk 向量，子 chunk 复用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import lancedb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
SNAP = ROOT / ".semantic-audit-snapshot"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def p_at_k(ids, labels, k=5):
    grades = [labels.get(cid, {}).get("grade", 0) for cid in ids[:k]]
    return sum(1 for g in grades if g >= 2) / k


def apply_threshold(scored, abs_t, rel):
    if not scored:
        return []
    top = scored[0][1]
    threshold = max(abs_t, top * rel)
    return [(cid, s) for cid, s in scored if s >= threshold]


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider
    from kerui_recruit.search.documents import build_candidate_document_b

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])

    retrieval = json.loads((SNAP / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    labels = json.loads((SNAP / "blind_labels.json").read_text(encoding="utf-8"))

    # 1) 冻结 DB 候选人 parsed_data（按 revision_id）
    conn = sqlite3.connect(SNAP / "recruit.sqlite3")
    parsed_by_rev = {}
    for rid, p in conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ):
        parsed_by_rev[rid] = json.loads(p) if p else {}
    conn.close()

    # 2) 读冻结索引所有 chunk
    db = lancedb.connect(str(SNAP / "search"))
    table = db.open_table("candidate_chunks")
    rows = table.search(None).limit(None).to_list()
    parent_rows = [r for r in rows if r["chunk_type"] == "parent"]
    child_rows = [r for r in rows if r["chunk_type"] == "child"]
    print(f"parent={len(parent_rows)} child={len(child_rows)}")

    # 3) 对父 chunk 生成 B vector_text
    b_texts = []
    for r in parent_rows:
        parsed = parsed_by_rev.get(r["revision_id"], {})
        b_texts.append(build_candidate_document_b(parsed)["vector_text"])

    # 4) 重新 embedding B 父 chunk
    async with httpx.AsyncClient(timeout=120) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])
        b_vectors: list[list[float]] = []
        for i in range(0, len(b_texts), 25):
            batch = b_texts[i:i + 25]
            b_vectors.extend(await emb.embed_documents(batch))
            if (i // 25) % 20 == 0:
                print(f"embedded {i + len(batch)}/{len(b_texts)}", flush=True)
    print(f"embedded total {len(b_vectors)} parent vectors")

    # 5) 写隔离 B 索引（父用新 vector，子复用原 vector）
    out_dir = ROOT / ".tmp-ab-index"
    import shutil
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_db = lancedb.connect(str(out_dir))
    new_parent = []
    for r, vec in zip(parent_rows, b_vectors):
        row = dict(r)
        row["vector"] = vec
        new_parent.append(row)
    out_db.create_table("candidate_chunks", data=new_parent + child_rows, mode="overwrite")
    print(f"wrote B index: parent={len(new_parent)} child={len(child_rows)}")

    # 6) 用 24 查询检索 A（冻结索引）与 B（隔离索引），算纯向量 P@5
    async with httpx.AsyncClient(timeout=120) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])

        def search_table(ltable, vector):
            return ltable.search(list(vector), vector_column_name="vector", query_type="vector").limit(100).to_list()

        a_table = db.open_table("candidate_chunks")
        b_table = out_db.open_table("candidate_chunks")

        for abs_t, rel in ((0.60, 0.90), (0.45, 0.85)):
            a_p5 = []
            b_p5 = []
            for qid, item in retrieval.items():
                if qid > "Q24":
                    continue
                query = item["intent"]
                qvec = await emb.embed_query(query)
                a_rows = search_table(a_table, qvec)
                b_rows = search_table(b_table, qvec)
                a_scored = [(alias(r["candidate_id"]), 1.0 / (1.0 + r["_distance"])) for r in a_rows if isinstance(r.get("_distance"), (int, float))]
                b_scored = [(alias(r["candidate_id"]), 1.0 / (1.0 + r["_distance"])) for r in b_rows if isinstance(r.get("_distance"), (int, float))]
                a_ids = [cid for cid, _ in apply_threshold(a_scored, abs_t, rel)]
                b_ids = [cid for cid, _ in apply_threshold(b_scored, abs_t, rel)]
                a_p5.append(p_at_k(a_ids, labels.get(qid, {})))
                b_p5.append(p_at_k(b_ids, labels.get(qid, {})))
            a_macro = round(sum(a_p5) / len(a_p5), 4)
            b_macro = round(sum(b_p5) / len(b_p5), 4)
            print(f"abs={abs_t} rel={rel}: A P@5={a_macro}  B P@5={b_macro}  delta={round(b_macro - a_macro, 4)}")


if __name__ == "__main__":
    asyncio.run(main())
