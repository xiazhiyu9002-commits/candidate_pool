"""在冻结快照上验证三个已知配对（Task 3 冻结配对回归）。"""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from kerui_recruit.match.candidate_view import build_candidate_view
from kerui_recruit.match.policy import evaluate_pair

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".semantic-audit-snapshot" / "recruit.sqlite3"


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# JD 匿名号（8位）= alias(jd_revision.id)[:8]
jd_rev_map = {}
for (rid,) in conn.execute(
    "SELECT r.id FROM jd_revision r JOIN jd j ON j.id=r.jd_id WHERE r.is_current=1 AND j.deleted_at IS NULL"
):
    jd_rev_map[alias(rid)[:8]] = rid

# 候选人匿名号（12位）= alias(candidate.id)
people_map = {}
for (cid,) in conn.execute("SELECT id FROM candidate"):
    people_map[alias(cid)] = cid

PAIRS = [
    ("f96cb81456e3", "d13894a1", "可入选"),
    ("42c0b000fc46", "56f942a1", "至少待核（不硬拒）"),
    ("9eeb2bf3737c", "543afa81", "只能待核（不直接推荐）"),
]

for cand_alias, jd_alias, expect in PAIRS:
    cid = people_map.get(cand_alias)
    rid = jd_rev_map.get(jd_alias)
    if not cid or not rid:
        print(f"[{cand_alias} -> {jd_alias}] 映射缺失 cid={cid} rid={rid}")
        continue

    jd_row = conn.execute("SELECT parsed_data FROM jd_revision WHERE id=?", (rid,)).fetchone()
    jd_parsed = json.loads(jd_row["parsed_data"]) if jd_row and jd_row["parsed_data"] else {}

    rev_rows = conn.execute(
        "SELECT rr.id, rr.created_at, rr.parsed_data FROM resume_revision rr "
        "JOIN resume_document rd ON rd.id=rr.document_id "
        "WHERE rd.candidate_id=? AND rr.is_current=1 AND rr.status='READY'",
        (cid,),
    ).fetchall()
    revisions = [
        SimpleNamespace(id=r["id"], created_at=parse_dt(r["created_at"]),
                        parsed_data=json.loads(r["parsed_data"]) if r["parsed_data"] else {})
        for r in rev_rows
    ]
    candidate_parsed = build_candidate_view(revisions, None)
    decision = evaluate_pair(jd_parsed, candidate_parsed)

    print(f"\n=== {cand_alias} -> {jd_alias}（期望 {expect}）===")
    print(f"  JD required_skills: {jd_parsed.get('required_skills')}")
    print(f"  JD must_skill_groups: {jd_parsed.get('must_skill_groups')}")
    print(f"  候选人技能: {candidate_parsed.get('skills')}")
    print(f"  eligibility: {decision.eligibility}  hard_reasons: {decision.hard_reasons}")
    print(f"  matched: {decision.matched_skills}  missing: {decision.missing_skills}")
