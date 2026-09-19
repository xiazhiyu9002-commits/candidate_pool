"""A/B the active candidate-profile prompt on 18 redacted, frozen records."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx

from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.resumes.profile import _PROFILE_PROMPT

from semantic_audit_blind_2026_09_17 import alias, redacted

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / ".semantic-audit-snapshot"

QUOTAS = {"BACKEND": 3, "DATA": 3, "ALGORITHM": 2, "FRONTEND": 2,
          "OPS": 2, "PRODUCT": 2, "MANAGEMENT": 2, "QA": 2}

B_PROMPT = """你是招聘顾问。依据脱敏的结构化简历证据，写一段中文候选人画像，只输出正文。控制在 170～220 字，最多三句话；超长时删去次要经历和技术词。
第一句先写候选人实际交付的业务方向和系统或产品类型。第二句写最近或最能证明能力的职责与项目，保留主导、负责、参与的事实等级。第三句只选 2～4 组对该候选人有判别力的技术或方法，说明它们如何用于交付；可并入第二句。对数仓/数据工程看业务主题、管道/建模/治理与分析交付的实际分工；对 Agent、算法、前端、运维、产品、管理分别看其实际交付物。不要在画像中提及无关方向或写“不是/而非”式比较。
数字仅在证据明确且有助岗位判断时写；没有数字就写业务范围、复杂度或交付边界。教育、知名公司、证书不可代替职责证据。不得推断未给出的规模、结果、岗位级别或管理责任，也不得把参与说成主导。文字客观、简洁，不写空泛赞美和求职建议。
结构化简历证据：
{evidence}"""


def evidence(parsed: dict) -> dict:
    experiences = [x for x in (parsed.get("experiences") or []) if isinstance(x, dict)]
    projects = [x for x in (parsed.get("projects") or []) if isinstance(x, dict)]
    names = {str(parsed.get(k) or "") for k in ("name", "school", "current_company")}
    names.update(str(x.get("company") or "") for x in experiences)
    names.update(str(x.get("school") or "") for x in (parsed.get("educations") or []) if isinstance(x, dict))
    return {
        "total_years": parsed.get("total_years"),
        "highest_degree": parsed.get("highest_degree"),
        "industry": redacted(parsed.get("industry"), names, 80),
        "current_title": redacted(parsed.get("current_title"), names, 80),
        "skills": [redacted(x, names, 40) for x in (parsed.get("skills") or [])[:28]],
        "summary": redacted(parsed.get("summary"), names, 250),
        "experiences": [{"title": redacted(x.get("title"), names, 80),
                          "summary": redacted(x.get("summary"), names, 380)} for x in experiences[:3]],
        "projects": [{"business_scene": redacted(x.get("business_scene"), names, 130),
                      "tech_stack": redacted(x.get("tech_stack"), names, 130),
                      "summary": redacted(x.get("summary"), names, 380)} for x in projects[:4]],
    }


def sample() -> list[dict]:
    connection = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    grouped: dict[str, dict[str, dict]] = {direction: {} for direction in QUOTAS}
    rows = connection.execute("""SELECT c.id,r.parsed_data FROM candidate c
        JOIN resume_document d ON d.candidate_id=c.id JOIN resume_revision r ON r.document_id=d.id
        WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL AND r.is_current=1 AND r.status='READY'""")
    for cid, raw in rows:
        parsed = json.loads(raw) if isinstance(raw, str) else raw or {}
        direction = parsed.get("direction")
        if direction in grouped and parsed.get("summary") and parsed.get("experiences") and parsed.get("projects"):
            grouped[direction].setdefault(alias(cid), parsed)
    connection.close()
    selected = []
    for direction, quota in QUOTAS.items():
        ids = sorted(grouped[direction], key=lambda cid: hashlib.sha256(cid.encode()).hexdigest())[:quota]
        selected.extend({"id": cid, "direction": direction, "evidence": evidence(grouped[direction][cid])} for cid in ids)
    if len(selected) != 18:
        raise RuntimeError(f"expected 18 profiles, got {len(selected)}")
    return selected


async def generate(client: httpx.AsyncClient, key: str, model: str, prompt: str) -> str:
    for attempt in range(3):
        try:
            response = await client.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "thinking": {"type": "disabled"}, "temperature": 0,
                      "max_tokens": 2500}, timeout=90)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if content and content.strip():
                return " ".join(content.split())
        except Exception:
            pass
        await asyncio.sleep(2 * (attempt + 1))
    return ""


async def main() -> None:
    samples = sample()
    config_dir = ROOT / ".dev-data/config"
    config = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(config["encrypted_api_key"])
    model = config["models"]["fast_text"]
    path = SNAPSHOT / "profile_ab.json"
    prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing = prior.get("records", {})
    if prior and prior.get("prompt_b_sha256") != hashlib.sha256(B_PROMPT.encode()).hexdigest():
        for record in existing.values():
            if record.get("B"):
                record["B1"] = record.pop("B")
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient() as client:
        async def one(item: dict) -> tuple[str, dict]:
            current = existing.get(item["id"], {})
            text = json.dumps(item["evidence"], ensure_ascii=False, separators=(",", ":"))
            async with semaphore:
                if not current.get("A"):
                    current["A"] = await generate(client, key, model,
                        _PROFILE_PROMPT.format(instruction="", previous="", evidence=text))
                if not current.get("B"):
                    current["B"] = await generate(client, key, model, B_PROMPT.format(evidence=text))
            current["direction"] = item["direction"]
            current["evidence"] = item["evidence"]
            return item["id"], current
        for completed in asyncio.as_completed([one(item) for item in samples]):
            cid, record = await completed
            existing[cid] = record
            print(cid, record["direction"], len(record["A"]), len(record["B"]), flush=True)
            path.write_text(json.dumps({"model": model, "prompt_a_sha256": hashlib.sha256(_PROFILE_PROMPT.encode()).hexdigest(),
                "prompt_b_sha256": hashlib.sha256(B_PROMPT.encode()).hexdigest(), "records": existing},
                ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
