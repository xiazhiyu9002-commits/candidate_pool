"""把机检不达标的候选人画像**确定性**压到限内，并触发索引重嵌。

背景（2026-09-21 核查）：机检是 ``providers/profile_spec.py:vet_profile()``（生成期护栏，不拦落库），
候选人侧要求「单段无换行、120~160 字、禁学历词、禁数字、末句不落在评价上」。
当前 1716 条当前版画像里 375 条未过：超上限 304、不足下限 32、评价式收尾 38、禁写数字 1。

本脚本只处理**能确定性收敛**的部分（不调模型）：
- 以分点为唯一事实源（沿用 ``pair_from_points`` / ``join_points_as_narrative`` 的契约），
  在分点序列上取「最长的、过机检的前缀」，必要时截断最后一个分点 → 逐字同源天然成立；
- 用 ``vet_profile`` 本身当判据（规则变了也不用改这里）；修不动的（例如不足下限、截断后仍触犯规则）
  一律**不动**，列为 ``needs_model`` 交回生成链路。

用法：

    # 只看能修多少、修成什么样（不写任何数据）
    py -3.12 scripts/compress_oversized_profiles_2026_09_21.py --dry-run

    # 执行：先备份受影响行的 parsed_data，再写回并入队索引同步
    py -3.12 scripts/compress_oversized_profiles_2026_09_21.py --execute

产物：``.tmp-plan/profile-compress-<mode>.json``（统计 + 样本 + 需要模型重生成的行）。
安全：``--execute`` 前会先把受影响行整份 ``parsed_data`` 备份到 ``.tmp-profile-compress-backup/``。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.core.paths import AppPaths  # noqa: E402
from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision  # noqa: E402
from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.providers.profile_pair import (  # noqa: E402
    _CLAUSE_TERMINATORS,
    ProfilePoint,
    join_points_as_narrative,
    split_profile_clauses,
)
from kerui_recruit.providers.profile_spec import vet_profile  # noqa: E402
from kerui_recruit.search.sync import enqueue_sync  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

NARRATIVE_LO, NARRATIVE_HI = 120, 160


def classify(issues: tuple[str, ...]) -> str:
    """把机检原因归类（一个违规行通常只命中一条）。"""
    joined = " ".join(issues)
    for key, token in (("over_long", "超过上限"), ("under_long", "不足下限"),
                       ("newline", "换行"), ("edu", "学历/学校"), ("number", "不该写的数字"),
                       ("evaluative", "评价式收尾")):
        if token in joined:
            return key
    return "other"


def _points_from(data: dict) -> list[ProfilePoint]:
    """读分点：优先结构化分点；缺失时按句读把整段拆成点（保证契约成立）。"""
    raw = data.get("ai_profile_points")
    points: list[ProfilePoint] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            text = str((item or {}).get("text") or "").strip() if isinstance(item, dict) else str(item).strip()
            if text:
                paths = list((item or {}).get("evidence_paths") or []) if isinstance(item, dict) else []
                points.append(ProfilePoint(text=text, evidence_paths=paths))
    if points:
        return points
    return [ProfilePoint(text=clause, evidence_paths=[]) for clause in split_profile_clauses(data.get("ai_profile_narrative") or "")]


def _trim_to_boundary(text: str, *, min_keep: int = 20) -> str:
    """尽量把截断点收到最近的句读边界，避免切在词中间；收得太短就保持硬截。

    外层还会再校验整段长度，这里只负责「好看一点」。
    """
    for index in range(len(text) - 1, -1, -1):
        if text[index] in _CLAUSE_TERMINATORS:
            candidate = text[:index + 1]
            return candidate if len(candidate) >= min_keep else text
    return text


def _best_prefix(points: list[ProfilePoint], limit: int) -> tuple[str, list[ProfilePoint]]:
    """最长的「前若干分点（必要时截断最后一个）」文本，长度不超过 limit。"""
    best_text, best_points = "", []
    for keep in range(1, len(points) + 1):
        text = join_points_as_narrative(points[:keep])
        if len(text) <= limit:
            if len(text) > len(best_text):
                best_text, best_points = text, list(points[:keep])
            continue
        head = points[:keep - 1]
        prefix = join_points_as_narrative(head)
        separator = 1 if prefix and not prefix.endswith(_CLAUSE_TERMINATORS) else 0
        room = limit - len(prefix) - separator
        if room <= 0:
            break
        truncated_text = _trim_to_boundary(points[keep - 1].text[:room])
        truncated = ProfilePoint(text=truncated_text,
                                 evidence_paths=list(points[keep - 1].evidence_paths))
        candidate = [*head, truncated]
        candidate_text = join_points_as_narrative(candidate)
        if len(candidate_text) <= limit and len(candidate_text) > len(best_text):
            best_text, best_points = candidate_text, candidate
        break
    return best_text, best_points


def shrink(narrative: str, points: list[ProfilePoint]) -> tuple[str, list[ProfilePoint]] | None:
    """返回能过机检的（narrative, points）；做不到时返回 None。"""
    if not points:
        return None
    text, kept = _best_prefix(points, NARRATIVE_HI)
    if len(text) < NARRATIVE_LO or not kept:
        return None
    if vet_profile(text, side="candidate"):
        return None
    if text == narrative:
        return None
    return text, kept


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".dev-data")
    parser.add_argument("--execute", action="store_true", help="真正写回并入队索引同步")
    parser.add_argument("--dry-run", action="store_true", help="只统计（默认行为）")
    parser.add_argument("--verify", action="store_true", help="只校验契约不变量（分点是否都逐字同源）")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    paths = AppPaths.from_root(data_root)
    engine = create_engine_for(paths.database)
    factory = sessionmaker(engine, expire_on_commit=False)

    stats: Counter[str] = Counter()
    reason_fixable: Counter[str] = Counter()
    reason_total: Counter[str] = Counter()
    samples: list[dict] = []
    needs_model: list[dict] = []
    fixes: list[dict] = []
    backup: list[dict] = []

    with factory() as session:
        rows = [
            (revision_id, parsed_data, candidate_id)
            for revision_id, parsed_data, candidate_id in session.execute(
                select(ResumeRevision.id, ResumeRevision.parsed_data, ResumeDocument.candidate_id)
                .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
                .where(ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY",
                       Candidate.deleted_at.is_(None))
            ).all()
        ]

    for revision_id, parsed, candidate_id in rows:
        data = parsed if isinstance(parsed, dict) else json.loads(parsed or "{}")
        narrative = str(data.get("ai_profile_narrative") or "")
        stats["rows"] += 1
        if args.verify:
            # 契约不变量：每个分点必须是 narrative 的逐字子串（reconcile_pair 的判定基础）。
            points = data.get("ai_profile_points") or []
            if any(str((item or {}).get("text") or "") not in narrative for item in points):
                stats["points_not_substring"] += 1
            elif narrative and not points:
                stats["no_points"] += 1
            continue
        violations = vet_profile(narrative, side="candidate")
        if not violations:
            stats["passed"] += 1
            continue
        stats["failed"] += 1
        reason = classify(violations)
        reason_total[reason] += 1
        result = shrink(narrative, _points_from(data))
        if result is None:
            stats["needs_model"] += 1
            needs_model.append({"revision": str(revision_id), "reason": reason,
                                "length": len(narrative)})
            continue
        text, kept = result
        stats["fixable"] += 1
        reason_fixable[reason] += 1
        fixes.append({
            "revision_id": str(revision_id), "candidate_id": str(candidate_id),
            "narrative": text,
            "points": [{"text": p.text, "evidence_paths": list(p.evidence_paths)} for p in kept],
            "before": len(narrative), "after": len(text), "reason": reason,
        })

    if args.verify:
        print(f"契约校验：当前版画像={stats['rows']} "
              f"分点非逐字同源={stats['points_not_substring']} "
              f"有正文无分点={stats['no_points']}")
        engine.dispose()
        return

    print(f"当前版画像={stats['rows']} 通过={stats['passed']} 未过={stats['failed']} "
          f"可确定性收敛={stats['fixable']} 需模型重生成={stats['needs_model']}")
    print(f"未过原因分布={dict(reason_total)}")
    print(f"其中可收敛的原因分布={dict(reason_fixable)}")
    for item in fixes[:5]:
        print(f"  样本 {item['revision_id'][:8]} {item['before']} -> {item['after']} 字 "
              f"（{item['reason']}）分点={len(item['points'])}")
        print(f"    {item['narrative']}")

    output = Path(args.output) if args.output else ROOT / ".tmp-plan" / (
        "profile-compress-execute.json" if args.execute else "profile-compress-dry-run.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.execute and fixes:
        backup_dir = ROOT / ".tmp-profile-compress-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with factory() as session, session.begin():
            for item in fixes:
                revision = session.get(ResumeRevision, item["revision_id"])
                if revision is None or not revision.parsed_data:
                    continue
                backup.append({"revision_id": item["revision_id"], "parsed_data": revision.parsed_data})
                latest = dict(revision.parsed_data)
                latest["ai_profile_narrative"] = item["narrative"]
                latest["ai_profile_points"] = item["points"]
                if item["points"]:
                    latest["ai_profile_compact"] = item["points"][0]["text"][:60]
                revision.parsed_data = latest
            # 入队索引同步：parsed_data 变了 → 父/子 chunk 文本与向量都要重投。
            for item in fixes:
                enqueue_sync(session, "candidate", item["candidate_id"])
        (backup_dir / f"profiles-{stamp}.json").write_text(
            json.dumps(backup, ensure_ascii=False), encoding="utf-8")
        print(f"已写回 {len(backup)} 行并入队索引同步；备份={backup_dir / f'profiles-{stamp}.json'}")
    elif args.execute:
        print("没有可确定性收敛的行，未做任何写入")

    output.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executed": bool(args.execute),
        "stats": dict(stats), "reason_total": dict(reason_total),
        "reason_fixable": dict(reason_fixable),
        "fixes": [{k: v for k, v in item.items() if k != "narrative"} | {"after": item["after"]}
                  for item in fixes],
        "needs_model": needs_model,
        "samples": [{"revision_id": item["revision_id"][:8], "before": item["before"],
                     "after": item["after"], "reason": item["reason"],
                     "narrative": item["narrative"]} for item in fixes[:10]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {output}")
    engine.dispose()


if __name__ == "__main__":
    main()
