"""把历史遗留的「僵尸任务」从死信里清出来（一次性数据修正）。

背景（2026-09-22 普查 `.dev-data`）：128 条 DEAD_LETTER 里有 **56 条**是
「Resume revision not found」/「blob 文件已不存在」——候选人被删掉后，队列里与重试中的
任务还会被领到、发现对象不存在、按 max_attempts 重试满 5 次才进死信。它们**不是解析失败**，
却被当成「解析质量差」的证据（计划 §1.5 就是这么误读的）。

根因已在 `CandidateDeletionService._cancel_pending_tasks()` 修掉（新增删除时取消）。
本脚本处理**修之前**已经躺在死信里的存量：按同一语义改为 `CANCELLED`，
让「死信数」重新只表示真正需要人看的失败。

判据只用错误原文里能证实的两种（对象不存在 / 文件不存在），不做任何推测性归类。

用法：
    py -3.12 scripts/cleanup_zombie_tasks_2026_09_22.py --data-root .dev-data --dry-run
    py -3.12 scripts/cleanup_zombie_tasks_2026_09_22.py --data-root .dev-data --apply
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
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from kerui_recruit.db.models import TaskEvent, TaskRecord  # noqa: E402
from kerui_recruit.db.session import create_engine_for  # noqa: E402
from kerui_recruit.sidecar import RuntimeArgs, build_settings  # noqa: E402

REPORT = ROOT / ".tmp-plan" / "cleanup-zombie-tasks-2026-09-22.json"


def is_zombie(error_message: str | None) -> bool:
    """只认「对象/文件已不存在」这两种可在错误原文里直接读到的情形。"""
    text = error_message or ""
    return ("not found" in text and "revision" in text) or "No such file or directory" in text


def main() -> int:
    parser = argparse.ArgumentParser(description="清理历史僵尸任务")
    parser.add_argument("--data-root", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token="0" * 64,
                                          data_root=data_root))
    engine = create_engine_for(settings.paths.database)
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)

    changed: list[dict] = []
    with factory() as session, session.begin():
        terminal = ("SUCCESS", "CANCELLED")
        # 死信与重试中都要清：RETRY_WAIT 里的僵尸还会继续空转。
        for task in session.scalars(
            select(TaskRecord).where(
                TaskRecord.status.in_(("DEAD_LETTER", "RETRY_WAIT")),
                TaskRecord.status.not_in(terminal),
            )
        ):
            if not is_zombie(task.error_message):
                continue
            changed.append({
                "task_id": task.id,
                "task_type": task.task_type,
                "from_status": task.status,
                "error_message": (task.error_message or "")[:120],
            })
            if args.apply:
                previous = task.status
                task.status = "CANCELLED"
                task.next_retry_at = None
                task.events.append(TaskEvent(
                    from_status=previous, to_status="CANCELLED", message="target_deleted_legacy"))

    by_type: dict[str, int] = {}
    for item in changed:
        by_type[item["task_type"]] = by_type.get(item["task_type"], 0) + 1

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "data_root": str(data_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "zombie_count": len(changed),
        "by_task_type": by_type,
        "items": changed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"识别僵尸任务 {len(changed)} 条，按类型：{by_type}")
    for item in changed[:10]:
        print(f"  {item['task_type']:<18} {item['from_status']:<12} {item['error_message']}")
    print(f"模式：{'已改为 CANCELLED' if args.apply else 'dry-run（未写库）'}")
    print(f"报告：{REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
