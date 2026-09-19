"""判决式评测第 2 步：把检索结果转成**盲测**判决输入 + 我的私有映射。

关键设计（否则结论不可信）：
1. 判断方只看 **JD 原文**，不看评测者写的查询文本 —— 避免把「查询写得好不好」误当成
   「检索系统好不好」。
2. 每个 JD 内把候选人**匿名化为字母编号**并按 JD 别名做确定性打乱，模式与名次不出现在
   输入里；映射单独落在 ``blinding_map.json``，由评测者持有。
3. 候选摘要给出「画像 + 年限学历 + 技能 + 工作经历 + 项目经历」，让判断基于真实履历。

产物：``.tmp-judge/judge_input_batch{1..N}.json``（交给子 Agent）与
``.tmp-judge/blinding_map.json``（评测者私有，不得进入子 Agent 的输入）。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev-data"
OUT = ROOT / ".tmp-judge"

BATCH_SIZE = 4          # 每个子 Agent 负责的 JD 数
NARRATIVE_CHARS = 200
SKILL_CHARS = 100
EXP_SUMMARY_CHARS = 70
PROJ_SUMMARY_CHARS = 100
MAX_EXPERIENCES = 3
MAX_PROJECTS = 2


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def clip(text, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def load_candidates(connection: sqlite3.Connection) -> dict[str, dict]:
    rows = connection.execute(
        "SELECT c.id, r.parsed_data, c.total_years, c.highest_degree "
        "FROM candidate c JOIN resume_document d ON d.candidate_id=c.id "
        "JOIN resume_revision r ON r.document_id=d.id "
        "WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL "
        "AND r.is_current=1 AND r.status='READY'"
    ).fetchall()
    found: dict[str, dict] = {}
    for cid, parsed, total_years, degree in rows:
        payload = parsed
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        if cid in found:
            continue  # 同一候选人多份 current 版本时取第一份
        found[cid] = {"parsed": payload, "total_years": total_years, "degree": degree}
    return found


def digest(entry: dict) -> str:
    p = entry["parsed"]
    years = p.get("total_years") or entry["total_years"]
    degree = p.get("highest_degree") or entry["degree"]
    school = p.get("school") or ""
    parts = [f"画像：{clip(p.get('ai_profile_narrative') or p.get('ai_profile_summary'), NARRATIVE_CHARS)}"]

    head = f"基本情况：{years or '未知'} 年经验 / {degree or '学历未知'}"
    if school:
        head += f" / {clip(school, 24)}"
    if p.get("location"):
        head += f" / 现居 {p['location']}"
    if p.get("current_company") or p.get("current_title"):
        head += f" / 当前 {clip(p.get('current_company'), 20)} {clip(p.get('current_title'), 24)}"
    parts.append(head)

    skills = p.get("skills") or []
    if skills:
        parts.append(f"技能：{clip('、'.join(str(s) for s in skills), SKILL_CHARS)}")

    experiences = [e for e in (p.get("experiences") or []) if isinstance(e, dict)]
    if experiences:
        lines = []
        for exp in experiences[:MAX_EXPERIENCES]:
            period = f"{exp.get('start_date') or '?'}~{exp.get('end_date') or '至今'}"
            lines.append(f"  · {period} {clip(exp.get('company'), 22)} {clip(exp.get('title'), 26)}："
                         f"{clip(exp.get('summary'), EXP_SUMMARY_CHARS)}")
        parts.append("工作经历：\n" + "\n".join(lines))

    projects = [x for x in (p.get("projects") or []) if isinstance(x, dict)]
    if projects:
        lines = []
        for proj in projects[:MAX_PROJECTS]:
            lines.append(f"  · {clip(proj.get('name'), 30)}｜技术栈 {clip(proj.get('tech_stack'), 60)}｜"
                         f"{clip(proj.get('summary'), PROJ_SUMMARY_CHARS)}")
        parts.append("项目经历：\n" + "\n".join(lines))

    # 注：不再附加索引侧的年限/学历标注（读取需 pylance；判断依据以简历解析结果为准）
    return "\n".join(parts)


def load_index_attrs(root: Path) -> dict[str, dict]:
    import lancedb

    table = lancedb.connect(str(root)).open_table("candidate_chunks")
    columns = ["candidate_id", "chunk_type", "location", "total_years", "highest_degree"]
    arrow = table.to_lance().to_table(columns=columns)
    data = {name: arrow.column(name).to_pylist() for name in arrow.column_names}
    attrs: dict[str, dict] = {}
    for i in range(arrow.num_rows):
        if data["chunk_type"][i] != "parent":
            continue
        attrs[data["candidate_id"][i]] = {
            "location": data["location"][i],
            "total_years": data["total_years"][i],
            "highest_degree": data["highest_degree"][i],
        }
    return attrs


def main() -> None:
    results = json.loads((OUT / "search_results.json").read_text(encoding="utf-8"))
    jds = json.loads((OUT / "jd_selected.json").read_text(encoding="utf-8"))
    queries = json.loads((OUT / "queries.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)
    universe = load_candidates(connection)
    connection.close()

    by_alias = {alias(cid): cid for cid in universe}
    blinding: dict[str, dict] = {}
    batches: list[dict] = []
    batch: dict[str, dict] = {}
    total_candidates = 0

    for i, jd in enumerate(jds, 1):
        jid = f"J{i:02d}"
        lists = results[jid]
        union: list[str] = []
        seen: set[str] = set()
        for phrasing in ("standard", "colloquial", "vague"):
            for mode in ("keyword", "vector", "hybrid"):
                for cand in lists[phrasing][mode]:
                    if cand not in seen:
                        seen.add(cand)
                        union.append(cand)

        rng = random.Random(jd["alias"])
        shuffled = union[:]
        rng.shuffle(shuffled)
        letters = {cand: chr(ord("A") + n) for n, cand in enumerate(shuffled)}

        entries = []
        for cand in shuffled:
            cid = by_alias.get(cand)
            if cid is None or cid not in universe:
                entries.append({"label": letters[cand], "digest": "（该候选人已不在库中）"})
                continue
            entry = dict(universe[cid])
            entry["cid"] = cid
            entries.append({"label": letters[cand], "digest": digest(entry)})

        jd_public = {
            "jd_id": jid,
            "company": jd["company"],
            "title": jd["title"],
            "min_years": jd["min_years"],
            "highest_degree": jd["highest_degree"],
            "location": jd["location"],
            "jd_text": jd["text"],
            "candidates": entries,
        }
        batch[jid] = jd_public
        blinding[jid] = {
            "jd_alias": jd["alias"],
            "company": jd["company"],
            "title": jd["title"],
            "letters": {letters[cand]: cand for cand in shuffled},
            "candidate_count": len(shuffled),
            "query_texts": queries[jid],
        }
        total_candidates += len(shuffled)

        if len(batch) >= BATCH_SIZE or i == len(jds):
            batches.append(batch)
            batch = {}

    for n, payload in enumerate(batches, 1):
        path = OUT / f"judge_input_batch{n}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        chars = path.stat().st_size
        print(f"batch{n}: JD={list(payload)} 字符≈{chars // 1000}K -> {path.name}")

    (OUT / "blinding_map.json").write_text(
        json.dumps(blinding, ensure_ascii=False, indent=1), encoding="utf-8")
    sizes = [b["candidate_count"] for b in blinding.values()]
    print(f"\nJD={len(jds)} 去重后候选总数={total_candidates} "
          f"每 JD 候选数 min/median/max={min(sizes)}/{sorted(sizes)[len(sizes)//2]}/{max(sizes)}")
    print(f"判断批次={len(batches)}  映射已写入 blinding_map.json（不得交给子 Agent）")


if __name__ == "__main__":
    main()
