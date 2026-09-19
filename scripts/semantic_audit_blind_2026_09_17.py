"""Create redacted evidence cards and blind-model labels for audit retrieval pools."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import httpx

from kerui_recruit.encryption.service import EncryptionService

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def redacted(text: object, names: set[str], limit: int) -> str:
    value = str(text or "")
    for name in sorted((name for name in names if len(name) >= 2), key=len, reverse=True):
        value = re.sub(re.escape(name), "[机构/姓名]", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[电话]", value)
    value = re.sub(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[邮箱]", value)
    value = re.sub(r"https?://\S+", "[网址]", value)
    value = re.sub(r"[\u4e00-\u9fffA-Za-z0-9（）()·-]{2,32}(?:有限公司|股份公司|集团公司|科技公司|技术公司|商业银行|大学)", "[机构]", value)
    for proper in ("PayPal", "京东众智", "北森", "奔驰中国", "广发银行", "博瑞尚格", "鲲鹏华清"):
        value = re.sub(re.escape(proper), "[机构]", value, flags=re.IGNORECASE)
    return " ".join(value.split())[:limit]


def card(parsed: dict, *, school_target: bool = False) -> dict:
    experiences = [e for e in (parsed.get("experiences") or []) if isinstance(e, dict)]
    projects = [p for p in (parsed.get("projects") or []) if isinstance(p, dict)]
    educations = [e for e in (parsed.get("educations") or []) if isinstance(e, dict)]
    names = {str(parsed.get(key) or "") for key in ("name", "current_company", "school")}
    names.update(str(e.get("company") or "") for e in experiences)
    names.update(str(e.get("school") or "") for e in educations)
    return {
        "年限": parsed.get("total_years"),
        "核心技能": [redacted(skill, names, 36) for skill in (parsed.get("skills") or [])[:12]],
        "画像": redacted(parsed.get("summary") or parsed.get("ai_profile_summary"), names, 180),
        "近期职责": [redacted(e.get("summary"), names, 115) for e in experiences[:2]],
        "项目证据": [redacted(p.get("summary") or p.get("business_scene"), names, 115) for p in projects[:2]],
        "目标院校": "是" if school_target else "否",
    }


def combine_revisions(records: list[dict]) -> dict:
    """Judge a person from every current READY resume, independent of hit mode."""
    result = dict(records[0])
    skills: list[str] = []
    experiences: list[dict] = []
    projects: list[dict] = []
    educations: list[dict] = []
    seen_exp: set[str] = set()
    seen_project: set[str] = set()
    for parsed in records:
        for skill in parsed.get("skills") or []:
            if skill and skill not in skills:
                skills.append(skill)
        for item in parsed.get("experiences") or []:
            if isinstance(item, dict):
                marker = str(item.get("summary") or "")
                if marker and marker not in seen_exp:
                    seen_exp.add(marker)
                    experiences.append(item)
        for item in parsed.get("projects") or []:
            if isinstance(item, dict):
                marker = str(item.get("summary") or "")
                if marker and marker not in seen_project:
                    seen_project.add(marker)
                    projects.append(item)
        educations.extend(e for e in parsed.get("educations") or [] if isinstance(e, dict))
    result["skills"] = skills
    result["experiences"] = experiences
    result["projects"] = projects
    result["educations"] = educations
    result["total_years"] = max((float(p.get("total_years")) for p in records
        if p.get("total_years") not in (None, "")), default=None)
    return result


def build_cards() -> dict:
    retrieval = json.loads((SNAPSHOT / "retrieval.json").read_text(encoding="utf-8"))["intents"]
    needed = {cid for qid, q in retrieval.items() if qid <= "Q24"
        for mode in q["modes"].values() for cid in mode["ids"]}
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    candidates: dict[str, list[dict]] = {}
    rows = connection.execute("""SELECT c.id,r.parsed_data FROM candidate c
        JOIN resume_document d ON d.candidate_id=c.id
        JOIN resume_revision r ON r.document_id=d.id
        WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL AND r.is_current=1 AND r.status='READY'
        ORDER BY r.created_at DESC, r.id DESC""")
    for cid, raw in rows:
        aid = alias(cid)
        if aid in needed:
            candidates.setdefault(aid, []).append(json.loads(raw) if isinstance(raw, str) else raw or {})
    candidates = {cid: combine_revisions(records) for cid, records in candidates.items()}
    connection.close()
    result = {}
    for qid, query in retrieval.items():
        if qid > "Q24":
            continue
        ids = {cid for mode in query["modes"].values() for cid in mode["ids"]}
        # Order is unrelated to mode or rank and fixed for repeatability.
        shuffled = sorted(ids, key=lambda cid: hashlib.sha256((qid + cid).encode()).hexdigest())
        result[qid] = {
            "intent": query["intent"],
            "cards": [{"id": cid, **card(candidates.get(cid, {}),
                school_target=any(e.get("school") == "北京大学" for e in
                    (candidates.get(cid, {}).get("educations") or []) if isinstance(e, dict)))}
                for cid in shuffled],
        }
    (SNAPSHOT / "blind_cards.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def judge_batch(client: httpx.AsyncClient, key: str, model: str,
                      qid: str, intent: str, cards: list[dict]) -> list[dict]:
    rubric = """你是独立招聘顾问，判断候选人对检索意图的相关性。只看下列脱敏证据，不得猜测。
评分：0=不相关；1=仅词面/技术词相关但职责不符；2=主要方向相关但有实质职责或资历缺口；3=业务方向、核心职责和关键技术高度吻合。技术词罗列不证明做过系统；主导、负责、参与不可混淆。若证据不足保守评分。
请为每张卡输出 JSON 对象：{"judgments":[{"id":"...","grade":0到3,"evidence":"最多8字","gap":"最多8字；无则空串"}]}。证据和缺口只写判断关键词，禁止复述简历；必须覆盖所有 id，顺序任意。不要输出其他文字。
检索意图：""" + intent + "\n候选人卡：" + json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
    for attempt in range(2):
        try:
            response = await client.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "messages": [{"role": "user", "content": rubric}],
                      "response_format": {"type": "json_object"},
                      "thinking": {"type": "disabled"}, "temperature": 0,
                      "max_tokens": 4000}, timeout=90)
            response.raise_for_status()
            payload = json.loads(response.json()["choices"][0]["message"]["content"])
            judgments = payload["judgments"]
            ids = {item["id"] for item in cards}
            if {item.get("id") for item in judgments} != ids or len(judgments) != len(ids):
                raise ValueError("judgment ID coverage differs from batch")
            if any(item.get("grade") not in (0, 1, 2, 3) for item in judgments):
                raise ValueError("invalid grade")
            return judgments
        except Exception as error:
            if attempt == 1:
                print(qid, "batch_failed", type(error).__name__, str(error)[:120], flush=True)
                return []
            await asyncio.sleep(2 * (attempt + 1))
    return []


async def main() -> None:
    prior_path = SNAPSHOT / "blind_cards.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    cards = build_cards()
    config_dir = ROOT / ".dev-data/config"
    config = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(config["encrypted_api_key"])
    model = config["models"]["fast_text"]
    output = SNAPSHOT / "blind_labels.json"
    labels = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    invalidated = 0
    for qid, entry in cards.items():
        old = {item["id"]: item for item in prior.get(qid, {}).get("cards", [])}
        for item in entry["cards"]:
            if item["id"] in labels.get(qid, {}) and item != old.get(item["id"]):
                labels[qid].pop(item["id"])
                invalidated += 1
    if invalidated:
        output.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
        print("invalidated_changed_cards", invalidated, flush=True)
    async with httpx.AsyncClient() as client:
        for qid, entry in cards.items():
            existing = labels.get(qid, {})
            remaining = [item for item in entry["cards"] if item["id"] not in existing]
            if not remaining:
                continue
            semaphore = asyncio.Semaphore(4)
            async def run(batch: list[dict]):
                async with semaphore:
                    result = await judge_batch(client, key, model, qid, entry["intent"], batch)
                if result or len(batch) == 1:
                    return result
                midpoint = len(batch) // 2
                halves = await asyncio.gather(run(batch[:midpoint]), run(batch[midpoint:]))
                return halves[0] + halves[1]
            batches = [remaining[i:i + 20] for i in range(0, len(remaining), 20)]
            for result in await asyncio.gather(*(run(batch) for batch in batches)):
                for item in result:
                    existing[item["id"]] = {"grade": item["grade"],
                        "evidence": str(item.get("evidence") or "")[:40],
                        "gap": str(item.get("gap") or "")[:40]}
            labels[qid] = existing
            output.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
            print(qid, len(existing), "/", len(entry["cards"]), flush=True)
    print("model", model, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
