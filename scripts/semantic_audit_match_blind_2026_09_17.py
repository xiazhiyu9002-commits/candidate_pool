"""Blind review of frozen, pseudonymous candidate/JD pairs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx

from kerui_recruit.encryption.service import EncryptionService
from semantic_audit_blind_2026_09_17 import alias, card, combine_revisions, redacted

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"
CONTROLS = (("9eeb2bf3737c", "543afa81"), ("f96cb81456e3", "d13894a1"),
            ("42c0b000fc46", "56f942a1"), ("318da8e9497c", "56f942a1"),
            ("b31d8cab5d64", "56f942a1"), ("4bbd6d724815", "56f942a1"))


def make_pairs() -> list[dict]:
    matches = json.loads((SNAPSHOT / "match_results.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    candidate_revisions: dict[str, list[dict]] = {}
    for cid, raw in connection.execute("""SELECT c.id,r.parsed_data FROM candidate c
        JOIN resume_document d ON d.candidate_id=c.id JOIN resume_revision r ON r.document_id=d.id
        WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL AND r.is_current=1 AND r.status='READY'
        ORDER BY r.created_at DESC,r.id DESC"""):
        candidate_revisions.setdefault(alias(cid), []).append(json.loads(raw))
    candidate_cards = {cid: card(combine_revisions(revisions)) for cid, revisions in candidate_revisions.items()}
    jd_cards = {}
    for rid, raw in connection.execute("""SELECT r.id,r.parsed_data FROM jd_revision r JOIN jd j ON j.id=r.jd_id
        WHERE r.is_current=1 AND r.status='READY' AND j.status='OPEN' AND j.deleted_at IS NULL"""):
        parsed = json.loads(raw)
        names = {str(parsed.get("company") or "")}
        jd_cards[alias(rid)[:8]] = {
            "岗位类型": redacted(parsed.get("title"), names, 60),
            "最低年限": parsed.get("min_years"),
            "最低学历": parsed.get("highest_degree"),
            "地点": parsed.get("location"),
            "核心技能": [redacted(x, names, 60) for x in (parsed.get("required_skills") or [])[:8]],
            "核心职责": [redacted(x, names, 150) for x in (parsed.get("core_duties") or [])[:6]],
        }
    connection.close()
    pairs: dict[tuple[str, str], set[str]] = {}
    for jd, modes in matches["forward"].items():
        for cid in modes["hybrid"]["ids"]:
            pairs.setdefault((cid, jd), set()).add("JD找人")
    for cid, modes in matches["reverse"].items():
        for jd in modes["hybrid"]["jds"]:
            pairs.setdefault((cid, jd), set()).add("人找JD")
    for cid, jd in CONTROLS:
        pairs.setdefault((cid, jd), set()).add("未入选对照")
    result = [{"pair": cid + ":" + jd, "candidate": candidate_cards[cid], "jd": jd_cards[jd],
               "sources": sorted(sources)} for (cid, jd), sources in sorted(pairs.items())
              if cid in candidate_cards and jd in jd_cards]
    (SNAPSHOT / "match_blind_cards.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def judge(client: httpx.AsyncClient, key: str, model: str, cards: list[dict]) -> list[dict]:
    # The model only receives pair ID and redacted evidence; no source, rank or score.
    blind = [{k: v for k, v in item.items() if k != "sources"} for item in cards]
    prompt = """你是独立招聘顾问。逐对核查脱敏岗位与候选人证据。看业务方向、核心职责、交付复杂度、本人作用、技术及年限/地点；技术词列表不能替代项目职责。输出 JSON：{"pairs":[{"pair":"原pair","verdict":"recommend或pending或reject","grade":0到3,"evidence":"最多18字","gap":"最多18字"}]}。recommend=核心职责有直接证据且硬条件满足；pending=有相关证据但关键职责或条件需核；reject=明显不符。覆盖全部 pair，不输出其他文字。\n""" + json.dumps(blind, ensure_ascii=False, separators=(",", ":"))
    for attempt in range(3):
        try:
            response = await client.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                      "temperature": 0, "max_tokens": 3500}, timeout=90)
            response.raise_for_status()
            result = json.loads(response.json()["choices"][0]["message"]["content"])["pairs"]
            if {x["pair"] for x in result} != {x["pair"] for x in cards}:
                raise ValueError("pair coverage mismatch")
            return result
        except Exception as error:
            if attempt == 2:
                print("batch_failed", type(error).__name__, flush=True)
                return []
            await asyncio.sleep(attempt + 1)
    return []


async def main() -> None:
    cards = make_pairs()
    config_dir = ROOT / ".dev-data/config"
    connection = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(connection["encrypted_api_key"])
    model = connection["models"]["fast_text"]
    output = SNAPSHOT / "match_blind_labels.json"
    labels = json.loads(output.read_text(encoding="utf-8")).get("labels", {}) if output.exists() else {}
    async with httpx.AsyncClient() as client:
        remaining = [card for card in cards if card["pair"] not in labels]
        batches = [remaining[i:i + 5] for i in range(0, len(remaining), 5)]
        for result in await asyncio.gather(*(judge(client, key, model, batch) for batch in batches)):
            for item in result:
                labels[item["pair"]] = item
    output.write_text(json.dumps({"model": model, "labels": labels}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("judged", len(labels), "/", len(cards), "model", model)


if __name__ == "__main__":
    asyncio.run(main())
