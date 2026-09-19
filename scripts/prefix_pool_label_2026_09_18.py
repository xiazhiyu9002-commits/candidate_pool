"""扩展池 + 补判：把各变体 top100 并池，对新候选人做盲评补 0-3 分。

- 池：``.tmp-prefix-eval/retrieval/*.json`` 里所有变体、所有模式的 alias 并集
  ∪ 快照 ``blind_labels.json`` 已标注 id；
- 补判：仅对池内尚无标签的 (query, candidate) 生成脱敏卡，用 DeepSeek 按同一
  rubric 盲评（``label_source="model_proxy"``）；
- 产出 ``.tmp-prefix-eval/blind_labels_ext.json``（prior 标签保留 ``label_source="prior_model"``）。

脱敏与判分逻辑与 ``semantic_audit_blind_2026_09_17.py`` 完全一致，便于并池后口径可比。
不碰 ``.dev-data``；密钥只用于请求头，不打印。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT))

from scripts.semantic_audit_blind_2026_09_17 import (  # noqa: E402
    alias,
    card,
    combine_revisions,
    judge_batch,
)

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
BATCH = 20
CONCURRENCY = 4


def load_pool() -> tuple[dict[str, list[str]], dict[str, str]]:
    files = sorted((EVAL_DIR / "retrieval").glob("*.json"))
    if not files:
        raise SystemExit(f"未找到变体结果：{EVAL_DIR / 'retrieval'}")
    pool: dict[str, list[str]] = {}
    intents: dict[str, str] = {}
    for path in files:
        retrieval = json.loads(path.read_text(encoding="utf-8"))
        for qid, entry in retrieval.items():
            if qid > "Q24":
                continue
            intents.setdefault(qid, entry.get("intent", ""))
            seen = pool.setdefault(qid, [])
            known = set(seen)
            for key, value in entry.items():
                if key == "intent" or not isinstance(value, list):
                    continue
                for cid in value:
                    if cid not in known:
                        known.add(cid)
                        seen.append(cid)
    return pool, intents


def candidate_records(needed: set[str]) -> dict[str, dict]:
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    rows = connection.execute(
        "SELECT c.id, r.parsed_data FROM candidate c "
        "JOIN resume_document d ON d.candidate_id=c.id "
        "JOIN resume_revision r ON r.document_id=d.id "
        "WHERE c.deleted_at IS NULL AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW') "
        "AND r.is_current=1 AND r.status='READY' "
        "ORDER BY r.created_at DESC, r.id DESC"
    )
    grouped: dict[str, list[dict]] = {}
    for cid, raw in rows:
        aid = alias(cid)
        if aid in needed:
            grouped.setdefault(aid, []).append(json.loads(raw) if isinstance(raw, str) else raw or {})
    connection.close()
    return {cid: combine_revisions(records) for cid, records in grouped.items()}


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService

    pool, intents = load_pool()
    prior_path = SNAPSHOT / "blind_labels.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}

    merged: dict[str, dict] = {}
    missing: dict[str, list[str]] = {}
    for qid, ids in pool.items():
        entry: dict[str, dict] = {}
        for cid, item in (prior.get(qid) or {}).items():
            filled = dict(item)
            filled["label_source"] = "prior_model"
            entry[cid] = filled
        missing[qid] = [cid for cid in ids if cid not in entry]
        merged[qid] = entry

    missing = {qid: ids for qid, ids in missing.items() if ids}
    total_missing = sum(len(ids) for ids in missing.values())
    print(f"queries={len(pool)} pool={sum(len(v) for v in pool.values())} "
          f"prior={sum(len(prior.get(q, {})) for q in pool)} to_judge={total_missing}", flush=True)

    if total_missing:
        records = candidate_records({cid for ids in missing.values() for cid in ids})
        absent = sorted({cid for ids in missing.values() for cid in ids} - set(records))
        if absent:
            print(f"absent_from_snapshot={len(absent)}", flush=True)

        config_dir = ROOT / ".dev-data/config"
        config = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
        key = EncryptionService(str(config_dir / "encryption.key")).decrypt(config["encrypted_api_key"])
        model = config["models"]["fast_text"]

        async with httpx.AsyncClient() as client:
            semaphore = asyncio.Semaphore(CONCURRENCY)

            async def run(qid: str, intent: str, batch: list[dict]) -> tuple[str, list[dict]]:
                async with semaphore:
                    return qid, await judge_batch(client, key, model, qid, intent, batch)

            for qid, ids in missing.items():
                cards = [
                    {"id": cid, **card(records.get(cid, {}), school_target=any(
                        e.get("school") == "北京大学"
                        for e in (records.get(cid, {}).get("educations") or []) if isinstance(e, dict)))}
                    for cid in ids if cid in records
                ]
                intent = intents.get(qid, "")
                tasks = [run(qid, intent, cards[i:i + BATCH]) for i in range(0, len(cards), BATCH)]
                for _, result in await asyncio.gather(*tasks):
                    for item in result:
                        merged[qid][item["id"]] = {
                            "grade": item["grade"],
                            "evidence": str(item.get("evidence") or "")[:40],
                            "gap": str(item.get("gap") or "")[:40],
                            "label_source": "model_proxy",
                        }
                print(qid, len(merged[qid]), "/", len(pool[qid]), flush=True)

    output = EVAL_DIR / "blind_labels_ext.json"
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {qid: len(entry) for qid, entry in merged.items()}
    gaps = {qid: len(pool[qid]) - counts.get(qid, 0) for qid in pool if counts.get(qid, 0) < len(pool[qid])}
    print(f"labels={sum(counts.values())} unlabeled_gaps={gaps or 'none'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
