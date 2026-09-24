"""只读诊断：AI 复核的「输入是否相同」vs「结论是否一致」。

复核 prompt 里喂给模型的**不是** JD 解析结果拼出的文本，而是
`resolve_jd_source(parsed, source_text, manual_overrides)` 的结果：
画像人工改过 → 画像文本；否则 → 按小节标注（【岗位职责】【优先项】【任职要求】）的 JD 原文；
原文缺失才退回 `_build_jd_text(parsed_data)`。

所以：若两个岗位侧输入完全一样，prompt 就完全一样 —— 此时结论不同只能是模型自身的不确定性。
本脚本用**真实的 resolve_jd_source** 计算每个岗位的复核输入，再与结论对照。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.match.review import resolve_jd_source  # noqa: E402

DB = (ROOT / ".dev-data" / "db" / "recruit.sqlite3").as_posix()
SHOW = 90


def clip(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SHOW else flat[:SHOW - 1] + "…"


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    task = con.execute(
        "select result_ref from task where task_type='MATCH_REVIEW' order by created_at desc limit 1"
    ).fetchone()
    items = json.loads(task["result_ref"])

    rows = []
    for i, item in enumerate(items, 1):
        mid = item.get("match_result_id")
        meta = con.execute(
            "select r.total_score, r.jd_revision_id, jd.company, jd.title, "
            "rv.parsed_data, rv.source_text, rv.manual_overrides "
            "from match_result r "
            "left join jd_revision rv on rv.id = r.jd_revision_id "
            "left join jd on jd.id = rv.jd_id where r.id = ?", (mid,)
        ).fetchone()
        if meta is None:
            continue
        try:
            parsed = json.loads(meta["parsed_data"]) if meta["parsed_data"] else {}
        except (TypeError, ValueError):
            parsed = {}
        try:
            overrides = json.loads(meta["manual_overrides"]) if meta["manual_overrides"] else None
        except (TypeError, ValueError):
            overrides = None
        review_input = resolve_jd_source(parsed, meta["source_text"], overrides)[0]
        rows.append({
            "i": i,
            "title": f"{meta['company'] or '-'}/{meta['title'] or '-'}",
            "score": float(meta["total_score"]),
            "verdict": item.get("verdict"),
            "failed": item.get("failed"),
            "input": review_input,
            "digest": hashlib.sha256(review_input.encode("utf-8")).hexdigest()[:8],
            "risks": len(item.get("risks") or item.get("cautions") or []),
            "sections": sum(
                len(item.get(key) or ())
                for key in ("project_match", "experience_match", "tech_match")
            ) or len(item.get("reasons") or []),
        })

    print(f"{'序':<3}{'结论':<11}{'总分':>8}{'输入哈希':>10}{'输入长度':>9}{'三段':>5}{'风险':>5}  岗位")
    for row in rows:
        print(f"{row['i']:<3}{str(row['verdict']):<11}{row['score']:>8.4f}{row['digest']:>10}"
              f"{len(row['input']):>9}{row['sections']:>5}{row['risks']:>5}  {row['title'][:36]}")

    print("\n=== 复核输入完全相同的分组（同输入 → 结论应当一致）===")
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["digest"], []).append(row)
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    if not duplicates:
        print("  没有完全相同的输入分组（说明这些岗位结构化后并不相同）")
    for digest, members in duplicates.items():
        verdicts = {m["verdict"] for m in members}
        flag = "  ← 结论不一致！" if len(verdicts) > 1 else ""
        print(f"\n  输入哈希 {digest}（{len(members)} 个岗位）{flag}")
        for m in members:
            print(f"    [{m['i']}] {m['verdict']:<10} 总分={m['score']:.4f}  {m['title'][:40]}")

    print("\n=== 复核输入的成对相似度（3-gram Jaccard，只列 >= 0.75 的）===")
    for a in range(len(rows)):
        for b in range(a + 1, len(rows)):
            ta, tb = rows[a]["input"], rows[b]["input"]
            sa = {ta[i:i + 3] for i in range(max(0, len(ta) - 2))}
            sb = {tb[i:i + 3] for i in range(max(0, len(tb) - 2))}
            if not (sa | sb):
                continue
            jac = len(sa & sb) / len(sa | sb)
            if jac >= 0.75:
                same = "相同" if rows[a]["verdict"] == rows[b]["verdict"] else "**不同**"
                print(f"  [{rows[a]['i']}]({rows[a]['verdict']}) vs [{rows[b]['i']}]({rows[b]['verdict']})"
                      f" = {jac:.3f}  结论{same}")
                print(f"       A: {rows[a]['title'][:44]}")
                print(f"       B: {rows[b]['title'][:44]}")

    print("\n=== 抽查：某两个高相似岗位的复核输入原文 ===")
    if len(rows) >= 2:
        for row in rows[:3]:
            print(f"\n[{row['i']}] {row['title']}  结论={row['verdict']}")
            print(f"    输入：{clip(row['input'])}")


if __name__ == "__main__":
    main()
