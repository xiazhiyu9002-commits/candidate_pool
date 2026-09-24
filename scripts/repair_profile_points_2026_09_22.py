"""修复存量画像分点：把「按逗号切碎」的历史分点按整体段落重算。

背景：`split_profile_clauses` 原先按 `[。！？；;，,\\n]+` 切分，**包含中文逗号**，
于是「3年后端经验，现任京东后端开发工程师。」被切成两个碎片分点。这些碎片在界面上
表现为「每隔一个标点就换行」，也会各自变成一个向量子 chunk、拉低检索粒度。

改动分三处（本脚本是第三处）：
1. **写入路径**已改为只按句末标点切分（`profile_pair._SENTENCE_BREAK`）；
2. **展示路径**已在读取响应时重算（`profile_pair.repaired_profile_view`）；
3. **本脚本**把可证明被切碎的存量分点落库，并入队重建索引——这样检索粒度也一起修好。

修复判据（`profile_pair.repair_profile_points`）：必须**同时**满足
「存在短于 12 字的分点」且「分点用『。』拼接后不等于整体段落」。
只按长度合并会误伤「7年后端研发 / 主导交易系统」这类短但合法的整句分点。

用法（**应用必须先关闭**）：

    # 只看差异，不写库
    py -3.12 scripts/repair_profile_points_2026_09_22.py --data-root .dev-data --dry-run

    # 确认差异后落库；每改一条会入队索引重建，下次启动应用时自动投影
    py -3.12 scripts/repair_profile_points_2026_09_22.py --data-root .dev-data --apply

产物：控制台摘要 + ``.tmp-plan/repair-profile-points-2026-09-22.json``。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision  # noqa: E402
from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.providers.profile_pair import repair_profile_points  # noqa: E402
from kerui_recruit.sidecar import RuntimeArgs, build_settings  # noqa: E402
from kerui_recruit.search.sync import enqueue_sync  # noqa: E402

REPORT = ROOT / ".tmp-plan" / "repair-profile-points-2026-09-22.json"
MAX_PRINTED_DIFFS = 30


def _points_of(parsed: dict) -> list[str]:
    return [
        str(item.get("text") or "") if isinstance(item, dict) else str(item)
        for item in (parsed.get("ai_profile_points") or [])
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="修复存量画像分点（按句末标点重算）")
    parser.add_argument("--data-root", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="只打印差异，不写库")
    group.add_argument("--apply", action="store_true", help="落库并入队索引重建")
    args = parser.parse_args()

    settings = build_settings(RuntimeArgs(
        host="127.0.0.1", port=1, token="0" * 64, data_root=args.data_root,
    ))
    engine = create_engine_for(settings.paths.database)
    factory = sessionmaker(engine, expire_on_commit=False)

    scanned = skipped_manual = skipped_no_points = 0
    changed: list[dict] = []

    with factory() as session:
        rows = session.execute(
            select(ResumeRevision, ResumeDocument.candidate_id)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .where(ResumeRevision.status == "READY")
        ).all()

        for revision, candidate_id in rows:
            scanned += 1
            parsed = dict(revision.parsed_data or {})
            if parsed.get("ai_profile_source") == "manual":
                # 人工编辑过的画像代表使用者的判断，任何自动改写都不允许碰。
                skipped_manual += 1
                continue
            stored = _points_of(parsed)
            if not stored:
                skipped_no_points += 1
                continue
            narrative = str(
                parsed.get("ai_profile_narrative") or parsed.get("ai_profile_summary") or ""
            )
            repaired = repair_profile_points(narrative, stored)
            if repaired is None:
                continue

            changed.append({
                "candidate_id": candidate_id,
                "revision_id": revision.id,
                "before": stored,
                "after": repaired,
            })
            if args.apply:
                parsed["ai_profile_points"] = [
                    {"text": text, "evidence_paths": []} for text in repaired
                ]
                revision.parsed_data = parsed
                # 分点变了 → 该候选人的子 chunk 集合变了 → 必须重建索引，
                # 否则向量库里仍是碎片粒度（这正是这次要一并修好的部分）。
                enqueue_sync(session, "candidate", candidate_id)

        if args.apply:
            session.commit()

    for item in changed[:MAX_PRINTED_DIFFS]:
        print(f"\n- {item['candidate_id']}")
        print(f"  修复前（{len(item['before'])} 条）：{' / '.join(item['before'])}")
        print(f"  修复后（{len(item['after'])} 条）：{' / '.join(item['after'])}")
    if len(changed) > MAX_PRINTED_DIFFS:
        print(f"\n…… 另有 {len(changed) - MAX_PRINTED_DIFFS} 条差异未打印，见报告文件")

    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "data_root": str(args.data_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scanned": scanned,
        "changed": len(changed),
        "skipped_manual": skipped_manual,
        "skipped_no_points": skipped_no_points,
        "changed_items": changed,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\n扫描 {scanned} 条：需要修复 {len(changed)} 条；"
        f"跳过人工编辑 {skipped_manual} 条、无分点 {skipped_no_points} 条"
    )
    print(f"模式：{'已落库 + 已入队索引重建（下次启动应用时投影）' if args.apply else 'dry-run（未写库）'}")
    print(f"报告：{REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
