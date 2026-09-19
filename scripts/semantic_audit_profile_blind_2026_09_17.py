"""Blindly rate frozen A/B profile outputs against redacted source evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from kerui_recruit.encryption.service import EncryptionService

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"


async def judge(client: httpx.AsyncClient, key: str, model: str, batch: list[dict]) -> list[dict]:
    prompt = ("你是独立招聘质量审查员。请根据脱敏原始证据，盲评两段候选人画像。P/Q 顺序随机，不能推测哪段是实验组。"
              "每段按以下四项分别给0-3分：事实准确（含职责等级、数字、无臆造），业务与系统信息，岗位区分力，简洁清晰。"
              "如有关键事实遗漏或幻觉，指出具体内容。只输出JSON对象："
              '{"items":[{"id":"id","P":{"facts":0,"business":0,"distinction":0,"concise":0,"issue":"短句"},'
              '"Q":{"facts":0,"business":0,"distinction":0,"concise":0,"issue":"短句"}}]}。'
              "每个id必须覆盖，评分应审慎，不能因为篇幅短就自动加分。材料："
              + json.dumps(batch, ensure_ascii=False, separators=(",", ":")))
    for attempt in range(3):
        try:
            response = await client.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                      "temperature": 0, "max_tokens": 2500}, timeout=90)
            response.raise_for_status()
            items = json.loads(response.json()["choices"][0]["message"]["content"])["items"]
            if {x["id"] for x in items} != {x["id"] for x in batch}:
                raise ValueError("incomplete batch")
            return items
        except Exception as error:
            if attempt == 2:
                print("failed", type(error).__name__, flush=True)
                return []
            await asyncio.sleep(attempt + 1)
    return []


async def main() -> None:
    records = json.loads((SNAPSHOT / "profile_ab.json").read_text(encoding="utf-8"))["records"]
    cards = []
    order = {}
    for cid, item in records.items():
        flipped = int(hashlib.sha256(cid.encode()).hexdigest(), 16) % 2 == 0
        order[cid] = {"P": "B" if flipped else "A", "Q": "A" if flipped else "B"}
        cards.append({"id": cid, "evidence": item["evidence"],
                      "P": item[order[cid]["P"]], "Q": item[order[cid]["Q"]]})
    config_dir = ROOT / ".dev-data/config"
    conn = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(conn["encrypted_api_key"])
    model = conn["models"]["fast_text"]
    async with httpx.AsyncClient() as client:
        batches = [cards[i:i + 3] for i in range(0, len(cards), 3)]
        responses = await asyncio.gather(*(judge(client, key, model, batch) for batch in batches))
    labels = {x["id"]: x for batch in responses for x in batch}
    output = {"model": model, "order": order, "labels": labels}
    (SNAPSHOT / "profile_blind_labels.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("judged", len(labels), "/", len(cards))


if __name__ == "__main__":
    asyncio.run(main())
