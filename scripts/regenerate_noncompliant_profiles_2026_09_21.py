"""用**模型重生成**无法确定性收敛的画像（机检未过），再触发索引重嵌。

背景：`scripts/compress_oversized_profiles_2026_09_21.py` 已把 375 条里能确定性压到限内的
301 条处理完（1341 → 1642 通过），剩下 74 条确定性手段做不到：
评价式收尾 36（要改写末句）、不足下限 32（要补写）、超长 5（截断后仍触犯规则）、禁写数字 1。

本脚本**复用生产回填链路**，不自己发明写库口径：
- 走 `BackfillService._backfill_one`（与「重新生成画像」按钮同一条路径），
  `profile_field/source_field/hash_field/stale_field` 全部照抄候选人的配置；
- 生成器内部自带 `produce_pair_with_vet`（不过机检就带原因回喂模型重写，最多 2 次）；
- `force=True` 跳过输入哈希短路（输入没变也要重写）；
- 写回后再用 `vet_profile` 复核一次，仍不过就如实报告（不静默算成功）；
- 最后 `enqueue_sync` + 排空，把改动投影进索引。

用法：

    py -3.12 scripts/regenerate_noncompliant_profiles_2026_09_21.py --dry-run   # 只列清单
    py -3.12 scripts/regenerate_noncompliant_profiles_2026_09_21.py --execute --concurrency 4

只处理「机检未过」的行；已通过的一律不动。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from kerui_recruit.core.paths import AppPaths  # noqa: E402
from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision  # noqa: E402
from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.providers.profile_spec import vet_profile  # noqa: E402
from kerui_recruit.runtime import build_runtime  # noqa: E402
from kerui_recruit.search.sync import enqueue_sync  # noqa: E402
from kerui_recruit.sidecar import RuntimeArgs, build_settings  # noqa: E402

CANDIDATE_PROFILE_FIELDS = {
    "profile_field": "ai_profile_summary",
    "source_field": "ai_profile_source",
    "hash_field": "ai_profile_input_hash",
    "stale_field": "ai_profile_stale",
}


def classify(issues: tuple[str, ...]) -> str:
    joined = " ".join(issues)
    for key, token in (("over_long", "超过上限"), ("under_long", "不足下限"),
                       ("newline", "换行"), ("edu", "学历/学校"), ("number", "不该写的数字"),
                       ("evaluative", "评价式收尾")):
        if token in joined:
            return key
    return "other"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / ".dev-data")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", default=str(ROOT / ".tmp-plan" / "profile-regenerate.json"))
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    paths = AppPaths.from_root(data_root)
    engine = create_engine_for(paths.database)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as session:
        targets = []
        for revision_id, parsed in session.execute(
            select(ResumeRevision.id, ResumeRevision.parsed_data)
            .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
            .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
            .where(ResumeRevision.is_current.is_(True), ResumeRevision.status == "READY",
                   Candidate.deleted_at.is_(None),
                   Candidate.status.not_in(("ARCHIVED", "PENDING_REVIEW")))
        ).all():
            data = parsed if isinstance(parsed, dict) else json.loads(parsed or "{}")
            issues = vet_profile(str(data.get("ai_profile_narrative") or ""), side="candidate")
            if issues:
                targets.append((str(revision_id), classify(issues)))
    print(f"机检未过、待模型重生成={len(targets)} 原因分布={dict(Counter(r for _, r in targets))}")
    if args.dry_run or not args.execute:
        print("dry-run：未做任何写入（加 --execute 执行）")
        engine.dispose()
        return

    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token="0" * 64, data_root=data_root))
    # 装配放在 try 之外：装配失败就直接抛出，不要被 finally 的报告逻辑掩盖。
    runtime = build_runtime(settings)
    backfill = runtime.services.backfill_service
    sync = runtime.services.index_sync_service
    results: Counter[str] = Counter()
    updated_ids: list[str] = []
    errors: list[dict] = []
    remaining: list[dict] = []
    try:
        pending = list(targets)
        lock = asyncio.Lock()

        async def worker() -> None:
            while True:
                async with lock:
                    if not pending:
                        return
                    revision_id, reason = pending.pop(0)
                with factory() as session:
                    candidate_id = session.scalar(
                        select(ResumeDocument.candidate_id)
                        .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                        .where(ResumeRevision.id == revision_id))
                    revision = session.get(ResumeRevision, revision_id)
                if revision is None or candidate_id is None:
                    results["missing"] += 1
                    continue
                try:
                    outcome, error = await backfill._backfill_one(
                        revision, generator=backfill.candidate_generator,
                        entity_type="candidate", force=True, **CANDIDATE_PROFILE_FIELDS)
                except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整批
                    outcome, error = "failed", {"revision_id": revision_id,
                                                "error": f"{type(exc).__name__}"}
                async with lock:
                    results[outcome] += 1
                if error:
                    async with lock:
                        errors.append({"revision_id": revision_id, "reason": reason, **error})
                    continue
                if outcome != "updated":
                    async with lock:
                        remaining.append({"revision_id": revision_id, "reason": reason,
                                          "why": f"backfill {outcome}"})
                    continue
                with factory() as session:
                    fresh = session.get(ResumeRevision, revision_id)
                    issues = vet_profile(
                        str((fresh.parsed_data or {}).get("ai_profile_narrative") or ""),
                        side="candidate")
                if issues:
                    async with lock:
                        remaining.append({"revision_id": revision_id, "reason": reason,
                                          "why": classify(issues)})
                    continue
                async with lock:
                    updated_ids.append(candidate_id)
                print(f"  ok {revision_id[:8]}（{reason} → 合规）", flush=True)

        await asyncio.gather(*(worker() for _ in range(max(1, args.concurrency))))

        if updated_ids:
            with factory() as session, session.begin():
                for candidate_id in updated_ids:
                    enqueue_sync(session, "candidate", candidate_id)
            print(f"已入队索引同步={len(updated_ids)} 待投影={sync.status()['pending']}")
            cycles = 0
            while sync.status()["pending"] and cycles < 200:
                applied = await sync.run_once(batch_size=25)
                cycles += 1
                print(f"  sync cycle={cycles} applied={applied} "
                      f"pending={sync.status()['pending']}", flush=True)
                if applied == 0:
                    break
            await asyncio.to_thread(runtime.services.search_service.optimize_pending)
    finally:
        final = sync.status()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "targets": len(targets), "results": dict(results),
            "updated_candidates": len(updated_ids), "errors": errors[:20],
            "still_violating": remaining[:40], "still_violating_count": len(remaining),
            "index": {"pending": final["pending"], "failed": final["failed"]},
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果={dict(results)} 已合规={len(updated_ids)} "
              f"仍不合规={len(remaining)} 索引待投影={final['pending']} 失败={final['failed']}")
        for item in remaining[:10]:
            print(f"  仍不合规 {item['revision_id'][:8]} {item['reason']} → {item['why']}")
        print(f"报告已写入 {output}")
        if runtime.providers.http_client is not None:
            await runtime.providers.http_client.aclose()
        if runtime.services.ai_manager is not None:
            await runtime.services.ai_manager.close()
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
