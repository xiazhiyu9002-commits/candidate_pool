"""只读：打印最近一次 MATCH_REVIEW 任务的完整结论（结果 JSON 原文）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id, status, result_ref, error_message, created_at from task "
        "where task_type='MATCH_REVIEW' order by created_at desc limit 3"
    ).fetchall()
    for row in rows:
        ref = row["result_ref"] or ""
        print(f"task={row['id']} status={row['status']} at={row['created_at']} "
              f"err={row['error_message']} ref_len={len(ref)}")
        if not ref:
            continue
        try:
            payload = json.loads(ref)
        except (TypeError, ValueError):
            print("  result_ref 不是 JSON：", ref[:500])
            continue
        items = payload if isinstance(payload, list) else payload.get("items") or []
        print(f"  结论条数={len(items)}")
        print(f"\n  {'序':<3}{'结论':<11}{'总分':>8}  {'岗位':<40}{'JD哈希':>8}")
        for i, item in enumerate(items, 1):
            mid = item.get("match_result_id") if isinstance(item, dict) else None
            meta = con.execute(
                "select r.total_score, jd.company, jd.title, rv.source_text "
                "from match_result r "
                "left join jd_revision rv on rv.id = r.jd_revision_id "
                "left join jd on jd.id = rv.jd_id "
                "where r.id = ?", (mid,)
            ).fetchone()
            if meta is None:
                print(f"  {i:<3}{'-':<11}{'-':>8}  (找不到 match_result {mid})")
                continue
            import hashlib
            text = meta["source_text"] or ""
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            label = f"{meta['company'] or '-'}/{meta['title'] or '-'}"
            verdict = item.get("verdict")
            failed = item.get("failed")
            print(f"  {i:<3}{verdict:<11}{str(meta['total_score']):>8}  {label[:38]:<40}{digest:>8}"
                  + ("  [failed]" if failed else ""))
        print()
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            # 兼容两种契约：新版三段（project/experience/tech）+ risks；历史结果用 reasons/cautions。
            three = ("project_match", "experience_match", "tech_match")
            sections = [line for key in three for line in (item.get(key) or [])]
            risks = item.get("risks") or item.get("cautions") or []
            print(f"  [{i}] {item.get('verdict')} 三段={len(sections)} 风险点={len(risks)}")
            for key, label in zip(three, ("项目", "经历", "技术")):
                for line in (item.get(key) or [])[:1]:
                    print(f"       {label}: {str(line)[:100]}")
            for line in (sections if not any(item.get(k) for k in three)
                         else (item.get("reasons") or []))[:1]:
                print(f"       理由: {str(line)[:100]}")
            for line in risks[:2]:
                print(f"       风险: {str(line)[:100]}")


if __name__ == "__main__":
    main()
