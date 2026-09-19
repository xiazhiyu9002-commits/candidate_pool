"""Stratified blind review of current candidate direction labels."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx

from kerui_recruit.encryption.service import EncryptionService
from semantic_audit_blind_2026_09_17 import alias, card, combine_revisions

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"
QUOTA = {"BACKEND": 6, "DATA": 6, "ALGORITHM": 6, "FRONTEND": 5,
         "OPS": 5, "PRODUCT": 5, "MANAGEMENT": 5, "QA": 4,
         "OTHER": 3, "None": 3}
BOUNDARY = ("0049647cc556", "16b76537f91d", "82461ef87c8c", "62e3cce46ff3",
            "6f4d796ed1d1", "9eeb2bf3737c", "f96cb81456e3", "7d5bf75cce11")


def sample() -> list[dict]:
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    rows: dict[str, list[dict]] = {}
    for cid, raw in connection.execute("""SELECT c.id,r.parsed_data FROM candidate c
        JOIN resume_document d ON d.candidate_id=c.id JOIN resume_revision r ON r.document_id=d.id
        WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL AND r.is_current=1 AND r.status='READY'
        ORDER BY r.created_at DESC,r.id DESC"""):
        rows.setdefault(alias(cid), []).append(json.loads(raw))
    connection.close()
    groups: dict[str, list[str]] = {direction: [] for direction in QUOTA}
    for cid, revisions in rows.items():
        direction = str(revisions[0].get("direction"))
        if direction in groups:
            groups[direction].append(cid)
    selected = set(BOUNDARY) & set(rows)
    for direction, count in QUOTA.items():
        ids = sorted(groups[direction], key=lambda cid: hashlib.sha256(cid.encode()).hexdigest())
        selected.update(ids[:count])
    result = []
    for cid in sorted(selected):
        revisions = rows[cid]
        evidence = card(combine_revisions(revisions))
        evidence.pop("目标院校", None)
        result.append({"id": cid, "stored": revisions[0].get("direction"),
                       "all_stored": sorted({str(x.get("direction")) for x in revisions}),
                       "card": evidence})
    (SNAPSHOT / "direction_blind_cards.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def judge_batch(client: httpx.AsyncClient, key: str, model: str, batch: list[dict]) -> list[dict]:
    blind = [{"id": item["id"], **item["card"]} for item in batch]
    prompt = """你是独立招聘顾问。只按最近和最核心的本人交付职责判定职业主方向，不能按技能词、学校、公司名决定。枚举：BACKEND=服务/API/数据平台与管道/Agent应用编排；DATA=数仓建模/BI/指标与业务分析交付；ALGORITHM=模型训练/算法研发/推理优化；FRONTEND=网页/客户端交互研发；OPS=运维/SRE/平台稳定性；QA=测试与质量工程；PRODUCT=产品规划与运营；MANAGEMENT=以团队/项目管理为主要交付；OTHER=上述之外；证据相当或不足可填null。输出 JSON：{"items":[{"id":"原id","direction":"枚举或null","confidence":"high/medium/low","evidence":"最多15字"}]}，覆盖全部id。不要看到或猜测原系统标签。\n""" + json.dumps(blind, ensure_ascii=False, separators=(",", ":"))
    for attempt in range(3):
        try:
            response = await client.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                      "temperature": 0, "max_tokens": 3500}, timeout=90)
            response.raise_for_status()
            result = json.loads(response.json()["choices"][0]["message"]["content"])["items"]
            if {x["id"] for x in result} != {x["id"] for x in batch}:
                raise ValueError("candidate coverage mismatch")
            return result
        except Exception as error:
            if attempt == 2:
                print("batch_failed", type(error).__name__, flush=True)
                return []
            await asyncio.sleep(attempt + 1)
    return []


async def main() -> None:
    cards = sample()
    config_dir = ROOT / ".dev-data/config"
    connection = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(connection["encrypted_api_key"])
    model = connection["models"]["fast_text"]
    async with httpx.AsyncClient() as client:
        batches = [cards[i:i + 12] for i in range(0, len(cards), 12)]
        results = await asyncio.gather(*(judge_batch(client, key, model, batch) for batch in batches))
    labels = {x["id"]: x for batch in results for x in batch}
    (SNAPSHOT / "direction_blind_labels.json").write_text(json.dumps({"model": model, "labels": labels}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("judged", len(labels), "/", len(cards), "model", model)


if __name__ == "__main__":
    asyncio.run(main())
