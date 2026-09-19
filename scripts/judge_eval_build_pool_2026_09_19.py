"""导出全部 READY 的 JD（原文 + 结构化要求），供「LLM 判断版」评测挑选样本。

与弱监督评测的区别：本评测的真值来自子 Agent 依据 **JD 原文** 做出的匹配判断，
因此需要 JD 原文完整落盘，而不是只落 ``required_skills`` 派生的覆盖率标签。

只读 .dev-data。产物：``.tmp-judge/jd_pool_all.json``。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-judge"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT j.id, j.company, j.title, r.source_text, r.parsed_data "
        "FROM jd j JOIN jd_revision r ON r.jd_id=j.id "
        "WHERE r.is_current=1 AND r.status='READY' ORDER BY j.company, j.title"
    ).fetchall()

    pool = []
    for jid, company, title, text, parsed in rows:
        payload = parsed
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        source = text if isinstance(text, str) else ""
        pool.append({
            "jd_id": jid,
            "alias": alias(jid),
            "company": company or "",
            "title": title or "",
            "text_len": len(source),
            "text": source,
            "min_years": payload.get("min_years"),
            "highest_degree": payload.get("highest_degree"),
            "location": payload.get("location"),
            "direction": payload.get("direction"),
            "required_skills": payload.get("required_skills") or [],
            "plus_skills": payload.get("plus_skills") or [],
        })
    connection.close()

    (OUT / "jd_pool_all.json").write_text(
        json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 READY JD = {len(pool)} -> {OUT / 'jd_pool_all.json'}")
    short = [p for p in pool if len(p["text"]) < 200]
    print(f"其中原文 <200 字符（判断信息量不足）={len(short)}")
    for p in pool:
        print(f"{p['alias']} | {p['company'][:14]:<14} | {p['title'][:40]:<40} | "
              f"{p['text_len']:>5} | 年限={str(p['min_years'])[:4]:<4} | {p['direction']}")


if __name__ == "__main__":
    main()
