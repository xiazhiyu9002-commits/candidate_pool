"""只读诊断：VIKKI 候选人的反向匹配为什么只有第一个岗位被判「推荐」。

要回答的问题：几个岗位的 JD 文本相同，为何 AI 复核结论不同？
需要看的事实：
1. 这几个 JD 的 `source_text` 是否真的一致（哈希比对）；
2. 它们的 `parsed_data` 是否一致（若不一致，配对评估与分差就有了来源）；
3. 匹配总分与分数构成是否不同；
4. AI 复核任务的结论是否存在失败/超时（失败会被标成 pending 而不是 recommend）。

安全：数据库只读打开；不写任何文件；候选人/岗位只按需打印。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".dev-data" / "db" / "recruit.sqlite3"


def short(value) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def main() -> None:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else "VIKKI"
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name")]
    print("表：", ", ".join(tables))

    rows = con.execute(
        "select id, display_name, status, total_years, highest_degree "
        "from candidate where display_name like ? or display_name like ?",
        (f"%{name_filter}%", f"%{name_filter.title()}%"),
    ).fetchall()
    print(f"\n候选人匹配 '{name_filter}'：{len(rows)} 条")
    for row in rows:
        print(f"  id={row['id']} name={row['display_name']} status={row['status']} "
              f"years={row['total_years']} degree={row['highest_degree']}")
    if not rows:
        return
    candidate_ids = [row["id"] for row in rows]

    placeholders = ",".join("?" * len(candidate_ids))
    runs = con.execute(
        f"select id, trigger, jd_revision_id, mode, query_text, created_at "
        f"from match_run where id in ("
        f"  select distinct run_id from match_result where candidate_id in ({placeholders})"
        f") order by created_at desc limit 6",
        candidate_ids,
    ).fetchall()
    print(f"\n相关 match_run：{len(runs)} 条（最近在前）")
    for run in runs:
        count = con.execute("select count(*) from match_result where run_id=?",
                            (run["id"],)).fetchone()[0]
        print(f"  run={run['id']} trigger={run['trigger']} jd_rev={short(run['jd_revision_id'])} "
              f"mode={run['mode']} results={count} at={run['created_at']}")

    if not runs:
        return
    latest = runs[0]
    print(f"\n=== 最近一次 run 的结果（run={latest['id']}，trigger={latest['trigger']}）===")
    results = con.execute(
        "select r.jd_revision_id, r.total_score, r.score_breakdown, r.reason, "
        "       rv.jd_id, rv.source_text, jd.company, jd.title "
        "from match_result r "
        "left join jd_revision rv on rv.id = r.jd_revision_id "
        "left join jd on jd.id = rv.jd_id "
        "where r.run_id = ? order by r.total_score desc",
        (latest["id"],),
    ).fetchall()
    print(f"结果 {len(results)} 条")
    for i, row in enumerate(results, 1):
        text = row["source_text"] or ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else "-"
        print(f"\n[{i}] {short(row['company'])}/{short(row['title'])}")
        print(f"    jd_revision={row['jd_revision_id']}  jd_id={row['jd_id']}")
        print(f"    总分={row['total_score']}  JD文本哈希={digest} 长度={len(text)}")
        print(f"    分数构成={json.dumps(row['score_breakdown'], ensure_ascii=False)}")
        print(f"    reason={short(row['reason'])}")

    print("\n=== 这几个 JD 修订的解析字段对比 ===")
    seen = set()
    for row in results:
        rev = row["jd_revision_id"]
        if not rev or rev in seen:
            continue
        seen.add(rev)
        data = con.execute("select parsed_data, status, is_current from jd_revision where id=?",
                           (rev,)).fetchone()
        parsed = {}
        try:
            parsed = json.loads(data["parsed_data"]) if data and data["parsed_data"] else {}
        except (TypeError, ValueError):
            parsed = {}
        keys = ("direction", "career_directions", "career_specializations", "business_directions",
                "min_years", "highest_degree", "location", "must_skill_groups", "required_skills",
                "exact_constraints")
        print(f"\n  revision={rev} status={data['status'] if data else '-'} current={data['is_current'] if data else '-'}")
        for key in keys:
            value = parsed.get(key)
            print(f"    {key} = {short(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)}")

    print("\n=== 这四个「看起来一样」的 JD 到底像到什么程度 ===")
    picked = []
    for row in results:
        title = f"{row['company'] or '-'}/{row['title'] or '-'}"
        if any(key in title for key in ("工程小队负责人", "工程团队负责人")):
            picked.append((title, row["jd_revision_id"], row["source_text"] or ""))
    for i, (title, rev, text) in enumerate(picked):
        print(f"\n[{i + 1}] {title}  长度={len(text)}")
        print(f"    开头：{short(' '.join(text.split()))}")
    if len(picked) >= 2:
        print("\n  两两字符 3-gram Jaccard 相似度：")
        for a in range(len(picked)):
            for b in range(a + 1, len(picked)):
                sa = {picked[a][2][i:i + 3] for i in range(max(0, len(picked[a][2]) - 2))}
                sb = {picked[b][2][i:i + 3] for i in range(max(0, len(picked[b][2]) - 2))}
                jac = len(sa & sb) / len(sa | sb) if sa | sb else 0.0
                print(f"      [{a + 1}] vs [{b + 1}] = {jac:.3f}")

    print("\n=== MATCH_REVIEW 任务 ===")
    tasks = con.execute(
        "select id, status, progress, error_message, result_ref, payload, created_at "
        "from task where task_type='MATCH_REVIEW' order by created_at desc limit 6"
    ).fetchall()
    for task in tasks:
        print(f"  task={task['id']} status={task['status']} progress={task['progress']} "
              f"ref={short(task['result_ref'])} err={short(task['error_message'])} at={task['created_at']}")
        print(f"    payload={short(task['payload'])}")

    print("\n=== blob 存储位置探测 ===")
    blobs = ROOT / ".dev-data" / "blobs"
    if blobs.exists():
        entries = sorted(blobs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        for entry in entries:
            print(f"  {entry.name}  {entry.stat().st_size}B  {entry}")


if __name__ == "__main__":
    main()
