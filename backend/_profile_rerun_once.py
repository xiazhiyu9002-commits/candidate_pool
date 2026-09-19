"""一次性脚本：对指定数据根 force 重跑画像，产出真双形态（整体段落 + 分点 + 浓缩）。

只调 ``build_runtime``（不启动 worker / index_sync，也不触发 ``optimize_pending``），
因此 ``search/.fts-dirty`` 不会被消费。

用法：
    python _profile_rerun_once.py --mode pilot          # 20 候选人 + 5 JD 小样
    python _profile_rerun_once.py --mode full           # 全量 force
    python _profile_rerun_once.py --mode verify         # 只读核验双形态覆盖

约束：不打印任何密钥；实体 id 以 sha256 前缀别名输出。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND / "src"))

from kerui_recruit.core.settings import Settings  # noqa: E402
from kerui_recruit.runtime import build_runtime  # noqa: E402

DEFAULT_ROOT = BACKEND.parent / ".dev-data"

CANDIDATE_DUAL = ("ai_profile_narrative", "ai_profile_points", "ai_profile_compact")
JD_DUAL = ("candidate_profile_narrative", "candidate_profile_points", "candidate_profile_compact")


def alias(entity_id: str) -> str:
    return hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:12]


def _filled(data: dict, key: str) -> bool:
    value = data.get(key)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def survey(database: Path) -> dict:
    conn = sqlite3.connect("file:" + database.as_posix() + "?mode=ro", uri=True)
    out: dict = {"candidate": {}, "jd": {}}
    for table, keys, bucket in (
        ("resume_revision", CANDIDATE_DUAL, "candidate"),
        ("jd_revision", JD_DUAL, "jd"),
    ):
        total = 0
        counts = {k: 0 for k in keys}
        multiline = 0
        for (raw,) in conn.execute(
            "select parsed_data from %s where is_current=1 and status='READY'" % table
        ):
            total += 1
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            for key in keys:
                if _filled(data, key):
                    counts[key] += 1
            narrative = data.get(keys[0])
            if isinstance(narrative, str) and "\n" in narrative:
                multiline += 1
        out[bucket] = {"total": total, "multiline": multiline, **counts}
    conn.close()
    return out


def print_survey(label: str, report: dict) -> None:
    print("[" + label + "]")
    for bucket, keys in (("candidate", CANDIDATE_DUAL), ("jd", JD_DUAL)):
        row = report[bucket]
        detail = ", ".join("%s=%d" % (k, row[k]) for k in keys)
        print(
            "   %-9s total=%d | narrative_with_newline=%d | %s"
            % (bucket, row["total"], row["multiline"], detail)
        )


def progress_printer(kind: str):
    seen = set()

    def report(percent: int) -> None:
        step = percent - percent % 10
        if step in seen:
            return
        seen.add(step)
        print("   [%s] %d%%" % (kind, step), flush=True)

    return report


def print_result(kind: str, result: dict) -> None:
    print(
        "   [%s] total=%d updated=%d skipped=%d failed=%d"
        % (kind, result["total"], result["updated"], result["skipped"], result["failed"])
    )
    for entry in result["errors"][:20]:
        entity = entry.get("entity_id")
        print(
            "     - %s id=%s code=%s error=%s details=%s"
            % (
                entry.get("entity_type"),
                alias(entity) if entity else "-",
                entry.get("code"),
                entry.get("error"),
                entry.get("details"),
            )
        )
    if len(result["errors"]) > 20:
        print("     ... 其余 %d 条失败记录已省略" % (len(result["errors"]) - 20))


async def run(root: Path, *, candidate_limit: int | None, jd_limit: int | None) -> None:
    # session_token 仅用于 sidecar HTTP 鉴权，本脚本不经 HTTP，占位即可。
    settings = Settings(data_root=root, session_token="one-off-script")
    runtime = build_runtime(settings)
    service = runtime.services.backfill_service
    if service is None:
        raise SystemExit("backfill_service 未装配")
    try:
        print("== 候选人（force=%s, limit=%s）" % (True, candidate_limit))
        candidates = await service.backfill_candidate_profiles(
            report=progress_printer("candidate"), force=True, limit=candidate_limit
        )
        print_result("candidate", candidates)

        print("== JD（force=%s, limit=%s）" % (True, jd_limit))
        jds = await service.backfill_jd_profiles(
            report=progress_printer("jd"), force=True, limit=jd_limit
        )
        print_result("jd", jds)
    finally:
        client = runtime.providers.http_client
        if client is not None:
            await client.aclose()
        if runtime.services.ai_manager is not None:
            await runtime.services.ai_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="画像全量重跑（真双形态）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--mode", choices=("pilot", "full", "verify"), default="pilot")
    parser.add_argument("--candidate-limit", type=int, default=None)
    parser.add_argument("--jd-limit", type=int, default=None)
    args = parser.parse_args()

    root = args.root.resolve()
    database = root / "db" / "recruit.sqlite3"
    if not database.exists():
        raise SystemExit("数据库不存在: %s" % database)
    print("data_root=%s dot_fts_dirty=%s" % (root, (root / "search" / ".fts-dirty").exists()))

    if args.mode == "verify":
        print_survey("重跑后", survey(database))
        return

    print_survey("重跑前", survey(database))
    if args.mode == "pilot":
        candidate_limit = 20 if args.candidate_limit is None else args.candidate_limit
        jd_limit = 5 if args.jd_limit is None else args.jd_limit
    else:
        candidate_limit = args.candidate_limit
        jd_limit = args.jd_limit
    asyncio.run(run(root, candidate_limit=candidate_limit, jd_limit=jd_limit))
    print_survey("重跑后", survey(database))


if __name__ == "__main__":
    main()
