"""画像分块 v3 消融：对比旧索引 / 仅双形态 / 双形态+前缀 三组的片段数与向量文本。

只用确定性文本构造（不调用模型），产出三组片段数、父/子分类与文本前缀对比；
若存在 .semantic-audit-snapshot 且配置了 SiliconFlow embedding，再跑同快照纯向量 P@5。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
SNAP = ROOT / ".semantic-audit-snapshot"


def _profile_points(data: dict) -> list[str]:
    explicit = data.get("ai_profile_points") or []
    points: list[str] = []
    for p in explicit:
        if isinstance(p, dict):
            t = str(p.get("text") or "").strip()
        else:
            t = str(p).strip()
        if t:
            points.append(t)
    if points:
        return points
    summary = str(data.get("ai_profile_summary") or "").strip()
    return [line.strip() for line in summary.splitlines() if line.strip()] if summary else []


def _compact(data: dict) -> str:
    from kerui_recruit.search.documents import _CHILD_PREFIX_MAX

    compact = str(data.get("ai_profile_compact") or "").strip()
    if compact:
        return compact[: _CHILD_PREFIX_MAX]
    points = _profile_points(data)
    return points[0][: _CHILD_PREFIX_MAX] if points else ""


def build_variants(data: dict) -> dict[str, dict]:
    """返回三组变体的子片段（不含父 chunk）：old / dual / dual_prefix。"""
    from kerui_recruit.search.documents import build_child_documents

    # 当前实现 = dual+prefix（画像分点 + 工作/项目 + compact 前缀）
    dual_prefix = build_child_documents(data)
    # dual（去前缀）与 old（去画像分点、去前缀）由 dual_prefix 派生，保证三组事实同源。
    dual = []
    old = []
    for child in dual_prefix:
        vec = child["vector_text"]
        prefix = _compact(data)
        no_prefix = vec[len(prefix) + 1:] if prefix and vec.startswith(prefix + " ") else vec
        d = dict(child)
        d["vector_text"] = no_prefix
        dual.append(d)
        if child["kind"] != "profile_point":
            o = dict(d)
            o["vector_text"] = no_prefix
            old.append(o)
    return {"old": old, "dual": dual, "dual_prefix": dual_prefix}


def _counts(children: list[dict]) -> Counter:
    return Counter(c["kind"] for c in children)


def segment_report(parsed_by_rev: dict[str, dict]) -> None:
    """按修订统计三组变体的片段数（父=1 每修订，子按 kind 分）。"""
    totals = {k: Counter() for k in ("old", "dual", "dual_prefix")}
    revisions = len(parsed_by_rev)
    for data in parsed_by_rev.values():
        variants = build_variants(data)
        for name, children in variants.items():
            totals[name] += _counts(children)
    print(f"revisions={revisions}")
    print(f"{'variant':<12} {'profile_point':>14} {'experience':>12} {'project':>10} {'total_child':>12}")
    for name in ("old", "dual", "dual_prefix"):
        c = totals[name]
        print(f"{name:<12} {c['profile_point']:>14} {c['experience']:>12} {c['project']:>10} {sum(c.values()):>12}")


def _snapshot_parsed() -> dict[str, dict] | None:
    db = SNAP / "recruit.sqlite3"
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    parsed_by_rev: dict[str, dict] = {}
    for rid, p in conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL "
        "AND c.status NOT IN ('ARCHIVED','PENDING_REVIEW')"
    ):
        parsed_by_rev[rid] = json.loads(p) if p else {}
    conn.close()
    return parsed_by_rev


def _devdata_parsed() -> dict[str, dict] | None:
    db = ROOT / ".dev-data" / "db" / "recruit.sqlite3"
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    parsed_by_rev: dict[str, dict] = {}
    for rid, p in conn.execute(
        "SELECT r.id, r.parsed_data FROM resume_revision r "
        "JOIN resume_document d ON d.id=r.document_id "
        "JOIN candidate c ON c.id=d.candidate_id "
        "WHERE r.is_current=1 AND r.status='READY' AND c.deleted_at IS NULL"
    ):
        parsed_by_rev[rid] = json.loads(p) if p else {}
    conn.close()
    return parsed_by_rev


def main() -> None:
    parsed = _snapshot_parsed() or _devdata_parsed()
    if not parsed:
        print("未找到可用数据快照（.semantic-audit-snapshot/recruit.sqlite3 或 .dev-data/db/recruit.sqlite3）。")
        print("本脚本的片段数对比需要一份只读数据快照；纯向量 P@5 消融另需 SiliconFlow embedding 配置。")
        sys.exit(1)
    segment_report(parsed)
    print("\n提示：P@5 消融需隔离索引 + re-embedding，见 scripts/evaluate_vector_ab.py 的同快照模式；")
    print("      本脚本只做确定性的片段数/文本前缀对比，不把未跑的结果写成收益。")


if __name__ == "__main__":
    main()
