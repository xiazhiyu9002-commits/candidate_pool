"""从 .dev-data 真实运行记录中抽取「真实查询集」与确定性弱监督标签。

背景：合成评测集 Q01–Q27 与真实查询分布差异巨大；本脚本把真实运行过的查询
（``match_run.query_text``）与真实 JD 结构化要求（``jd_revision.parsed_data``）
组合成一套**确定性、无 LLM、可复现**的离线评测。

标签口径（全部由 ``required_skills`` 派生，不使用任何模型判断）：
- 条目：``required_skills`` 每个条目按 or/或// 拆成若干备选，命中任一备选即该条目满足。
- 条目有效性：先在全库上算命中率，剔除「零命中」（LLM 抽出的不可逐字匹配长句）与
  「命中率 > 0.85」（无区分度）的条目；只在有效条目上算覆盖率。
- 覆盖率 coverage = 命中条目数 / 有效条目数。
  grade = 3（coverage >= 1.0）/ 2（>= 0.6）/ 1（>= 0.4）/ 0（其余）。
  不使用「全部条目必须命中」的合取门：实测该门的中位通过率仅 0.001（约 1.5 人），
  会把相关集压到不可评测的规模。
- ``plus_*`` 命中数单独记录为辅助诊断，不参与 grade。
- 硬条件（``min_years`` / ``highest_degree`` / ``location``）单独记录，供「带过滤检索」对照。

只读打开 .dev-data；不写任何生产数据。产物全部使用别名 ID（sha256[:12]）。
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.search.degrees import degrees_at_least, normalize_degree  # noqa: E402
from kerui_recruit.search.query import has_skill  # noqa: E402

DEV = ROOT / ".dev-data"
EVAL_DIR = ROOT / ".tmp-real-eval"

# 备选切分：英文 or / 中文或 / 斜杠 / 顿号 / 逗号 / 竖线
_ALT_RE = re.compile(r"\s*(?:/|／|\||、|,|，|\bor\b|或)\s*", re.IGNORECASE)
# 必须被视为「不可逐字匹配的长句要求」：长度阈值
_VERBATIM_MIN_LEN = 16

# 管理/架构类信号（用于 Q17 类比桶）
_MGMT_RE = re.compile(r"管理|负责人|架构|技术团队|团队负责人|leader|head of|principal|staff", re.IGNORECASE)
# 条目有效性：剔除「零命中」（LLM 抽出的不可逐字匹配长句）与「命中率 > 0.85」（无区分度）
_MIN_ENTRY_HITS, _MAX_ENTRY_RATE = 1, 0.85
# 覆盖率分级带：(下界, grade)
_GRADE_BANDS = ((1.0, 3), (0.6, 2), (0.4, 1))


def alias(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def alternatives(entry: str) -> list[str]:
    parts = [p.strip() for p in _ALT_RE.split(entry or "")]
    return [p for p in parts if len(p) >= 2]


def flatten_plus(parsed: dict) -> list[str]:
    pooled: list[str] = []
    for key in ("plus_skills", "plus_project_types", "plus_industry"):
        value = parsed.get(key)
        if isinstance(value, str):
            pooled.append(value)
        elif isinstance(value, (list, tuple)):
            pooled.extend(str(v) for v in value if v)
    return [p for p in pooled if len(p) >= 2]


def entry_matches(text: str, entry: str) -> bool:
    return any(has_skill(text, alt) for alt in alternatives(entry))


def load_universe(connection: sqlite3.Connection) -> dict[str, dict]:
    """活跃候选人（AVAILABLE + 未删除 + 当前 READY 版本）及其全量简历文本。"""
    rows = connection.execute(
        "SELECT c.id, c.total_years, c.highest_degree, r.raw_text, r.parsed_data "
        "FROM candidate c JOIN resume_document d ON d.candidate_id=c.id "
        "JOIN resume_revision r ON r.document_id=d.id "
        "WHERE c.status='AVAILABLE' AND c.deleted_at IS NULL "
        "AND r.is_current=1 AND r.status='READY'"
    ).fetchall()
    universe: dict[str, dict] = {}
    for cid, total_years, degree, raw_text, parsed in rows:
        text = raw_text if isinstance(raw_text, str) and raw_text.strip() else ""
        if not text and parsed:
            text = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)
        universe[cid] = {
            "text": text.casefold(),
            "raw_len": len(text),
            "total_years": total_years,
            "degree": normalize_degree(degree),
        }
    return universe


def load_index_attrs(root: Path) -> dict[str, dict]:
    """从索引父 chunk 取过滤字段（与检索时实际使用的列一致）。"""
    import lancedb

    table = lancedb.connect(str(root)).open_table("candidate_chunks")
    columns = ["candidate_id", "chunk_type", "location", "preferred_location",
               "preferred_locations", "total_years", "highest_degree", "direction"]
    try:
        arrow = table.to_lance().to_table(columns=columns)
    except Exception:
        arrow = table.to_arrow()
        arrow = arrow.drop([c for c in arrow.column_names if c not in columns])
    data = {name: arrow.column(name).to_pylist() for name in arrow.column_names}
    attrs: dict[str, dict] = {}
    for i in range(arrow.num_rows):
        if data["chunk_type"][i] != "parent":
            continue
        attrs[data["candidate_id"][i]] = {
            "location": data["location"][i],
            "preferred_location": data["preferred_location"][i],
            "preferred_locations": data["preferred_locations"][i],
            "total_years": data["total_years"][i],
            "highest_degree": data["highest_degree"][i],
            "direction": data["direction"][i],
        }
    return attrs


def load_queries(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT query_text, jd_revision_id, trigger, created_at FROM match_run "
        "WHERE query_text IS NOT NULL AND trim(query_text) <> '' ORDER BY created_at"
    ).fetchall()
    grouped: dict[str, dict] = {}
    for text, revision_id, trigger, created_at in rows:
        entry = grouped.setdefault(text, {"runs": 0, "revisions": [], "triggers": set()})
        entry["runs"] += 1
        entry["triggers"].add(trigger)
        if revision_id:
            entry["revisions"].append(revision_id)
    queries = []
    for text, entry in grouped.items():
        revisions = list(dict.fromkeys(entry["revisions"]))
        queries.append({
            "text": text,
            "runs": entry["runs"],
            "triggers": sorted(entry["triggers"]),
            "jd_revision_id": revisions[-1] if revisions else None,
            "revision_candidates": len(revisions),
        })
    queries.sort(key=lambda q: (-q["runs"], q["text"]))
    return queries


def load_jd(connection: sqlite3.Connection, revision_id: str) -> dict | None:
    row = connection.execute(
        "SELECT parsed_data FROM jd_revision WHERE id=?", (revision_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    parsed = row[0]
    return json.loads(parsed) if isinstance(parsed, str) else parsed


def main() -> None:
    EVAL_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(f"file:{DEV / 'db' / 'recruit.sqlite3'}?mode=ro", uri=True)

    universe = load_universe(connection)
    attrs = load_index_attrs(DEV / "search")
    indexed = set(attrs)
    common = sorted(indexed & set(universe))
    print(f"universe(sqlite)={len(universe)} indexed={len(indexed)} 交集={len(common)}")

    queries = load_queries(connection)
    labeled = [q for q in queries if q["jd_revision_id"]]
    print(f"distinct query_text={len(queries)} 其中带 jd_revision_id={len(labeled)}")

    out_queries, out_labels, stats = [], {}, {}
    order = sorted(common)
    entry_cache: dict[str, frozenset[str]] = {}

    def entry_hits(entry: str) -> frozenset[str]:
        cached = entry_cache.get(entry)
        if cached is None:
            cached = frozenset(cid for cid in order if entry_matches(universe[cid]["text"], entry))
            entry_cache[entry] = cached
        return cached

    def band(coverage: float) -> int:
        for lower, grade in _GRADE_BANDS:
            if coverage >= lower:
                return grade
        return 0

    for index, query in enumerate(labeled, start=1):
        qid = f"R{index:03d}"
        parsed = load_jd(connection, query["jd_revision_id"])
        if not parsed:
            continue
        must_entries = [e for e in (parsed.get("required_skills") or []) if isinstance(e, str) and e.strip()]
        if not must_entries:
            continue
        plus_items = flatten_plus(parsed)

        must_hits = {e: entry_hits(e) for e in must_entries}
        max_hits = _MAX_ENTRY_RATE * len(order)
        effective = [e for e in must_entries if _MIN_ENTRY_HITS <= len(must_hits[e]) <= max_hits]
        if not effective:
            continue

        coverage_count: collections.Counter[str] = collections.Counter()
        for entry in effective:
            coverage_count.update(must_hits[entry])
        plus_count: collections.Counter[str] = collections.Counter()
        for item in plus_items:
            plus_count.update(entry_hits(item))

        effective_n = len(effective)
        grades: dict[str, int] = {}
        for cid in order:
            grade = band(coverage_count.get(cid, 0) / effective_n)
            if grade:
                grades[cid] = grade
        positives = len(grades)
        strong = sum(1 for g in grades.values() if g == 3)
        ge2 = sum(1 for g in grades.values() if g >= 2)
        # 已废弃的「全部 must 条目命中」合取门，仅为审计留存对照值
        all_must = sum(1 for cid in order if all(cid in must_hits[e] for e in must_entries))

        verbatim = sum(1 for e in must_entries if len(e) > _VERBATIM_MIN_LEN and not any(
            ch.isascii() and ch.isalpha() for ch in e))
        title = str(parsed.get("title") or "")
        skill_blob = " ".join(must_entries)
        buckets = []
        if parsed.get("min_years") or parsed.get("highest_degree") or parsed.get("location"):
            buckets.append("hard")
        if _MGMT_RE.search(title) or _MGMT_RE.search(skill_blob):
            buckets.append("mgmt")
        if not buckets:
            buckets.append("plain")

        out_labels[qid] = {alias(cid): g for cid, g in grades.items()}
        stats[qid] = {
            "alias": alias(query["text"]),
            "source": "jd_match",
            "runs": query["runs"],
            "triggers": query["triggers"],
            "revision_candidates": query["revision_candidates"],
            "text_len": len(query["text"]),
            "title": title,
            "buckets": buckets,
            "must_entries": len(must_entries),
            "effective_entries": effective_n,
            "dropped_entries": len(must_entries) - effective_n,
            "verbatim_entries": verbatim,
            "plus_items": len(plus_items),
            "positives": positives,
            "positives_ge2": ge2,
            "positives_ge3": strong,
            "positive_ratio": round(positives / len(order), 4),
            "strong": strong,
            "plus_avg": round(sum(plus_count.get(cid, 0) for cid in grades) / positives, 2) if positives else 0.0,
            "all_must_ratio": round(all_must / len(order), 4),
            "usable": effective_n >= 1 and positives > 0,
            "hard": {
                "min_years": parsed.get("min_years"),
                "degree": parsed.get("highest_degree"),
                "location": parsed.get("location"),
                "direction": parsed.get("direction"),
            },
        }
        out_queries.append({"qid": qid, "text": query["text"], **stats[qid]})

    unlabeled = [
        {"qid": f"U{i:03d}", "alias": alias(q["text"]), "text": q["text"], "runs": q["runs"]}
        for i, q in enumerate((q for q in queries if not q["jd_revision_id"]), start=1)
    ]
    bd_rows = connection.execute(
        "SELECT query FROM bd_search_session WHERE query IS NOT NULL AND trim(query) <> ''"
    ).fetchall()
    bd = [{"qid": f"B{i:03d}", "alias": alias(r[0]), "text": r[0]} for i, r in enumerate(bd_rows, start=1)]

    (EVAL_DIR / "real_queries.json").write_text(
        json.dumps(out_queries, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVAL_DIR / "real_labels.json").write_text(
        json.dumps(out_labels, ensure_ascii=False), encoding="utf-8")
    (EVAL_DIR / "label_stats.json").write_text(
        json.dumps({"labeled": stats, "unlabeled": unlabeled, "bd": bd,
                    "universe": len(order)}, ensure_ascii=False, indent=2), encoding="utf-8")

    usable = [q for q in out_queries if q["usable"]]
    bucket_count = collections.Counter(b for q in usable for b in q["buckets"])
    grade_dist = collections.Counter(g for labels in out_labels.values() for g in labels.values())
    positives = [q["positives"] for q in out_queries]
    dropped = [q["dropped_entries"] for q in out_queries]
    print(f"标注查询={len(out_queries)} 可用(>=1 有效条目且有正例)={len(usable)}")
    print(f"无 JD 关联的真实查询={len(unlabeled)} BD 短查询={len(bd)}")
    print(f"可用查询桶分布={dict(bucket_count)}")
    print(f"grade 分布(仅>0)={dict(sorted(grade_dist.items()))}")
    if positives:
        print(f"每查询正例数 min/median/max={min(positives)}/"
              f"{sorted(positives)[len(positives)//2]}/{max(positives)}")
        print(f"每查询被剔除条目数 min/median/max={min(dropped)}/"
              f"{sorted(dropped)[len(dropped)//2]}/{max(dropped)}")
        print(f"对照：全部 must 命中的通过率 min/median/max="
              f"{min(q['all_must_ratio'] for q in out_queries):.3f}/"
              f"{sorted(q['all_must_ratio'] for q in out_queries)[len(out_queries)//2]:.3f}/"
              f"{max(q['all_must_ratio'] for q in out_queries):.3f}")
    hard_usable = [q for q in usable if "hard" in q["buckets"]]
    print(f"可用且含硬条件={len(hard_usable)}")
    # recall@k 的理论上限：相关集远大于 k 时，指标被天花板压制
    for label, field in (("grade>=1", "positives"), ("grade>=2", "positives_ge2"), ("grade==3", "positives_ge3")):
        counts = sorted(q[field] for q in out_queries)
        med = counts[len(counts) // 2]
        zero = sum(1 for n in counts if n == 0)
        ceilings = [min(1.0, 50 / n) if n else 0.0 for n in counts]
        print(f"  {label}: 每查询相关数 min/median/max={counts[0]}/{med}/{counts[-1]} 空集={zero} "
              f"recall@50 上限 median={sorted(ceilings)[len(ceilings)//2]:.3f}")
    for q in usable[:6]:
        print(f"  {q['qid']} {q['buckets']} must={q['must_entries']}(有效{q['effective_entries']}) "
              f"plus={q['plus_items']} 正例={q['positives']} strong={q['strong']} title={q['title']}")
    connection.close()


if __name__ == "__main__":
    main()
