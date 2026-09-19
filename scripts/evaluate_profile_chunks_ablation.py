"""画像分块 v3 P@5 消融：旧索引 vs 仅双形态 vs 双形态+前缀。

复用 evaluate_vector_ab.py 的同快照模式：读取冻结快照 + SiliconFlow embedding，
在隔离索引重建三组子向量并复算纯向量 P@5。父向量三组共享；子向量文本未变时复用
冻结向量（零成本），仅新文本（画像分点 / 加前缀后的经历项目）触发 embedding。
写隔离目录 .tmp-profile-chunks-ab，不碰 .dev-data。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import httpx
import lancedb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
SNAP = ROOT / ".semantic-audit-snapshot"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _profile_points(data: dict) -> list[str]:
    explicit = data.get("ai_profile_points") or []
    points: list[str] = []
    for p in explicit:
        if isinstance(p, dict):
            t = str(p.get("text") or "").strip()
        else:
            t = str(p).strip()
        if t:
            points.append(t)
    if points:
        return points
    summary = str(data.get("ai_profile_summary") or "").strip()
    return [line.strip() for line in summary.splitlines() if line.strip()] if summary else []


def _compact(data: dict) -> str:
    compact = str(data.get("ai_profile_compact") or "").strip()
    if compact:
        return compact
    points = _profile_points(data)
    return points[0][:60] if points else ""


def build_variants(data: dict) -> dict[str, list[dict]]:
    from kerui_recruit.search.documents import build_child_documents

    dual_prefix = build_child_documents(data)
    prefix = _compact(data)
    dual: list[dict] = []
    old: list[dict] = []
    for child in dual_prefix:
        vec = child["vector_text"]
        no_prefix = vec[len(prefix) + 1:] if prefix and vec.startswith(prefix + " ") else vec
        d = dict(child)
        d["vector_text"] = no_prefix
        dual.append(d)
        if child["kind"] != "profile_point":
            o = dict(d)
            o["vector_text"] = no_prefix
            old.append(o)
    return {"old": old, "dual": dual, "dual_prefix": dual_prefix}


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService
    from kerui_recruit.providers.siliconflow import SiliconFlowEmbeddingProvider

    settings = json.loads((ROOT / ".dev-data/config/settings.json").read_text(encoding="utf-8"))
    key = EncryptionService(str(ROOT / ".dev-data/config/encryption.key")).decrypt(settings["siliconflow_api_key"])
    retrieval = json.loads((SNAP / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    labels = json.loads((SNAP / "blind_labels.json").read_text(encoding="utf-8"))

    import sqlite3
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

    db = lancedb.connect(str(SNAP / "search"))
    table = db.open_table("candidate_chunks")
    rows = table.search(None).limit(None).to_list()
    parent_rows = [r for r in rows if r["chunk_type"] == "parent"]
    # 冻结向量按 vector_text 复用：文本未变则零成本。
    frozen_by_text = {r["vector_text"]: r["vector"] for r in rows if r.get("vector_text")}
    print(f"snapshot: parent={len(parent_rows)} total={len(rows)} frozen_texts={len(frozen_by_text)}", flush=True)

    variants = {name: {} for name in ("old", "dual", "dual_prefix")}
    for rid, data in parsed_by_rev.items():
        built = build_variants(data)
        for name, children in built.items():
            variants[name][rid] = children

    counts = {name: Counter(c["kind"] for c in sum(v.values(), [])) for name, v in variants.items()}
    for name in ("old", "dual", "dual_prefix"):
        print(f"{name}: {dict(counts[name])}", flush=True)

    async with httpx.AsyncClient(timeout=180) as client:
        emb = SiliconFlowEmbeddingProvider(
            api_key=key, client=client,
            base_url=settings["siliconflow_base_url"], model=settings["siliconflow_embedding_model"])

        # 收集各变体需要新 embedding 的文本（冻结已有则复用）
        missing_texts: set[str] = set()
        child_meta: dict[str, dict[str, dict]] = {name: {} for name in variants}
        for name, by_rev in variants.items():
            for rid, children in by_rev.items():
                for c in children:
                    key = f"{rid}:{c['kind']}:{c.get('sequence')}"
                    child_meta[name][key] = {"rid": rid, "kind": c["kind"], "vector_text": c["vector_text"],
                                             "keyword_index_text": c.get("keyword_index_text") or ""}
                    if c["vector_text"] not in frozen_by_text:
                        missing_texts.add(c["vector_text"])

        missing = list(missing_texts)
        print(f"需要新 embedding 的文本数：{len(missing)}", flush=True)
        new_vec_by_text: dict[str, list[float]] = {}
        for i in range(0, len(missing), 25):
            batch = missing[i:i + 25]
            vecs = await emb.embed_documents(batch)
            for t, v in zip(batch, vecs):
                new_vec_by_text[t] = v
            if (i // 25) % 20 == 0:
                print(f"  embedded {i + len(batch)}/{len(missing)}", flush=True)

        def resolve_vec(text: str) -> list[float]:
            return frozen_by_text.get(text) or new_vec_by_text[text]

        # 写隔离索引
        out_base = ROOT / ".tmp-profile-chunks-ab"
        if out_base.exists():
            shutil.rmtree(out_base)
        out_tables: dict[str, object] = {}
        for name in ("old", "dual", "dual_prefix"):
            out_dir = out_base / name
            out_db = lancedb.connect(str(out_dir))
            new_rows = [dict(r) for r in parent_rows]  # 父行原样复用（含 vector）
            for key, meta in child_meta[name].items():
                rid = meta["rid"]
                candidate_id = next((p["candidate_id"] for p in parent_rows if p["revision_id"] == rid), rid)
                new_rows.append({
                    "id": key,
                    "candidate_id": candidate_id,
                    "revision_id": rid,
                    "keyword_text": meta["vector_text"],
                    "keyword_index_text": meta["keyword_index_text"],
                    "vector_text": meta["vector_text"],
                    "body_index_text": "",
                    "chunk_type": "child",
                    "parent_id": rid,
                    "vector": resolve_vec(meta["vector_text"]),
                })
            out_db.create_table("candidate_chunks", data=new_rows, mode="overwrite")
            out_tables[name] = out_db.open_table("candidate_chunks")
            print(f"{name}: wrote {len(new_rows)} rows", flush=True)

        def apply_threshold(scored, abs_t, rel):
            if not scored:
                return []
            top = scored[0][1]
            threshold = max(abs_t, top * rel)
            return [(cid, s) for cid, s in scored if s >= threshold]

        def p_at_k(ids, qid, k=5):
            judged = labels.get(qid, {})
            grades = [int(judged.get(cid, {}).get("grade", 0)) for cid in ids[:k]]
            return sum(1 for g in grades if g >= 2) / k

        for abs_t, rel in ((0.60, 0.90), (0.45, 0.85)):
            p5 = {name: [] for name in ("old", "dual", "dual_prefix")}
            for qid, item in retrieval.items():
                if qid > "Q24":
                    continue
                qvec = await emb.embed_query(item["intent"])
                for name in ("old", "dual", "dual_prefix"):
                    rows = out_tables[name].search(list(qvec), vector_column_name="vector", query_type="vector").limit(100).to_list()
                    scored = [(alias(r["candidate_id"]), 1.0 / (1.0 + r["_distance"])) for r in rows if isinstance(r.get("_distance"), (int, float))]
                    ids = [cid for cid, _ in apply_threshold(scored, abs_t, rel)]
                    p5[name].append(p_at_k(ids, qid))
            line = "  ".join(f"{name}={round(sum(v) / len(v), 4)}" for name, v in p5.items())
            print(f"abs={abs_t} rel={rel}: {line}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
