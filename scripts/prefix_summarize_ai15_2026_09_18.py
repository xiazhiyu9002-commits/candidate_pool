"""方案 1 输入：用 AI 把候选人画像浓缩成 ≤15 字前缀（带缓存、可续跑）。

对冻结快照中每个 current+READY revision，取同一事实源（优先 ``ai_profile_summary``，
缺失时由 PROFILE_INPUT_FIELDS 结构化字段拼装）交给 DeepSeek 生成 ≤15 字浓缩前缀。

落盘 ``.tmp-prefix-eval/prefix_ai15.json``：
``{revision_id: {"compact": str, "input_hash": str, "source": "ai"|"fallback"}}``。
input_hash 未变且 source=ai 时跳过（幂等/可续跑）。密钥只用于请求头，不打印、不落盘。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SNAPSHOT = ROOT / ".semantic-audit-snapshot"
EVAL_DIR = ROOT / ".tmp-prefix-eval"
OUTPUT = EVAL_DIR / "prefix_ai15.json"
MAX_CHARS = 15
CONCURRENCY = 8

_PROMPT = """你是资深招聘顾问。根据下面的候选人结构化画像证据，浓缩出一条 ≤15 个汉字的检索前缀，只保留「工作年限 + 核心方向/职业定位」。
规则：
1. 只能来自证据，不得虚构年限、技术、行业或职级；
2. 总长度不超过 15 个汉字（英文技术词按 1 个字符计），不要标点结尾；
3. 禁止出现姓名、公司名、学校名、联系方式；
4. 禁止空泛评价（如「优秀」「资深」「能力强」），必须是可检索的方向词；
5. 只返回 JSON，不要 markdown、不要解释：{{"compact":"≤15字前缀"}}
结构化画像证据：
{evidence}"""


def build_evidence(data: dict) -> str:
    """优先用已生成画像，缺失时按 PROFILE_INPUT_FIELDS 拼装结构化证据。"""
    summary = str(data.get("ai_profile_summary") or "").strip()
    if summary:
        return " ".join(summary.split())
    from kerui_recruit.resumes.profile import PROFILE_INPUT_FIELDS

    payload = {key: data.get(key) for key in PROFILE_INPUT_FIELDS if data.get(key) not in (None, "", [])}
    return json.dumps(payload, ensure_ascii=False, default=str)


def input_hash(evidence: str) -> str:
    return hashlib.sha256(evidence.encode("utf-8")).hexdigest()


def fallback_compact(data: dict, evidence: str) -> str:
    summary = str(data.get("ai_profile_summary") or "").strip()
    source = summary.splitlines()[0] if summary else evidence
    return " ".join(source.split())[:MAX_CHARS]


async def summarize(client: httpx.AsyncClient, key: str, model: str, evidence: str) -> str | None:
    for attempt in range(2):
        try:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": _PROMPT.format(evidence=evidence)}],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "temperature": 0,
                    "max_tokens": 200,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = json.loads(response.json()["choices"][0]["message"]["content"])
            compact = " ".join(str(payload["compact"]).split())
            if not compact:
                raise ValueError("empty compact")
            return compact
        except Exception as error:
            if attempt == 1:
                print("summarize_failed", type(error).__name__, str(error)[:120], flush=True)
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


def load_revisions() -> dict[str, dict]:
    conn = sqlite3.connect(SNAPSHOT / "recruit.sqlite3")
    rows = conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ).fetchall()
    conn.close()
    return {rid: (json.loads(raw) if isinstance(raw, str) else raw or {}) for rid, raw in rows}


async def main() -> None:
    from kerui_recruit.encryption.service import EncryptionService

    config_dir = ROOT / ".dev-data/config"
    config = json.loads((config_dir / "ai-providers.json").read_text(encoding="utf-8"))["connections"][0]
    key = EncryptionService(str(config_dir / "encryption.key")).decrypt(config["encrypted_api_key"])
    model = config["models"]["fast_text"]

    revisions = load_revisions()
    cache = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    print(f"revisions={len(revisions)} cached={len(cache)}", flush=True)

    pending: list[tuple[str, str, dict]] = []
    skipped = 0
    for rid, data in revisions.items():
        evidence = build_evidence(data)
        digest = input_hash(evidence)
        cached = cache.get(rid)
        if cached and cached.get("source") == "ai" and cached.get("input_hash") == digest:
            skipped += 1
            continue
        pending.append((rid, evidence, data))
    print(f"pending={len(pending)} skipped={skipped}", flush=True)

    limit = int(os.environ.get("PREFIX_EVAL_LIMIT", "0") or 0)
    if limit:
        pending = pending[:limit]

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        async def run(rid: str, evidence: str, data: dict) -> None:
            async with semaphore:
                compact = await summarize(client, key, model, evidence)
                if compact:
                    cache[rid] = {"compact": compact[:MAX_CHARS],
                                  "input_hash": input_hash(evidence), "source": "ai"}
                else:
                    cache[rid] = {"compact": fallback_compact(data, evidence),
                                  "input_hash": input_hash(evidence), "source": "fallback"}

        tasks = [run(rid, evidence, data) for rid, evidence, data in pending]
        for i in range(0, len(tasks), 50):
            await asyncio.gather(*tasks[i:i + 50])
            OUTPUT.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  done {min(i + 50, len(tasks))}/{len(tasks)}", flush=True)

    OUTPUT.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    current = {rid: cache[rid] for rid in revisions if rid in cache}
    sources = Counter(item["source"] for item in current.values())
    lengths = [len(item["compact"]) for item in current.values()]
    within = sum(1 for n in lengths if n <= MAX_CHARS)
    leaked = 0
    for rid, data in revisions.items():
        item = cache.get(rid)
        if not item:
            continue
        compact = item["compact"]
        names = {str(data.get("name") or ""), str(data.get("current_company") or "")}
        if any(name and len(name) >= 2 and name in compact for name in names):
            leaked += 1
    lengths_sorted = sorted(lengths)
    median = lengths_sorted[len(lengths_sorted) // 2] if lengths_sorted else 0
    print(f"total={len(current)} sources={dict(sources)}", flush=True)
    print(f"within_{MAX_CHARS}={within / len(current):.4f} median_len={median} max_len={max(lengths) if lengths else 0}",
          flush=True)
    print(f"name_or_company_leak={leaked} model={model}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
