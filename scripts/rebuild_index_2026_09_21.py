"""阶段 4 合批重建：把「画像更新 + 词表扩容 + location_terms 归一化」一次投影到位。

背景（见 ``docs/superpowers/specs/2026-09-21-query-parse-hybrid-body-design.md`` §阶段 4）：
画像已全量重算、outbox 里积压着待投影实体；词表扩容与 ``location_terms`` 归一化又都是
索引列内容变更。三者天然合批：**一次全量重投影同时覆盖，不要拆成两次**。

升 ``INDEX_CHUNK_VERSION`` 后 ``plan_index_upgrade`` 判定为「就地修复」：
embedding 模型与向量维度没变，只补列 + 刷新 metadata，旧行全程可读可搜，
新行边填边收敛，队列排空即完成（不归档、不换向量口径、不需要 --app-stopped 的离线迁移）。

用法（**应用必须已关闭**；本脚本会写索引与 SQLite 的同步记录）：

    py -3.12 scripts/rebuild_index_2026_09_21.py --data-root .dev-data
    py -3.12 scripts/rebuild_index_2026_09_21.py --data-root .dev-data --batch 25

产物：控制台进度 + ``.tmp-plan/rebuild-2026-09-21.json``
（版本元数据、计数、耗时、FTS 前后探针、``location_terms`` 归一化抽查）。
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.search.cities import normalize_location_terms  # noqa: E402
from kerui_recruit.search.contracts import CandidateFilters  # noqa: E402
from kerui_recruit.search.lancedb_index import (  # noqa: E402
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)
from kerui_recruit.search.upgrade import plan_index_upgrade  # noqa: E402
from kerui_recruit.search.sync import enqueue_sync  # noqa: E402
from kerui_recruit.sidecar import RuntimeArgs, build_settings  # noqa: E402
from kerui_recruit.core.paths import AppPaths  # noqa: E402
from kerui_recruit.providers.factory import embedding_identity  # noqa: E402
from kerui_recruit.runtime import build_runtime  # noqa: E402

# FTS 前后探针：覆盖「城市归一化」与「词表别名」两类新能力，另加一个对照词。
PROBES = ("深圳", "数仓", "数据仓库", "前端", "java")
PROBE_LIMIT = 500
# 文档侧别名展开的落地证据：同一概念组的两个写法应当**同时在**关键词列里出现。
ALIAS_PAIRS = ("数仓", "数据仓库", "前端", "前端开发", "电商", "电子商务")
ALIAS_COOCCURRENCE = (("数仓", "数据仓库"), ("前端", "前端开发"), ("电商", "电子商务"))


def alias(value: str) -> str:
    """候选人 ID 一律以 sha256 别名落报告，不写原始 ID。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def read_metadata(root: Path) -> dict:
    for name in ("candidate-index-metadata.json",):
        path = root / name
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    return {}


def count_rows(path: Path, query: str) -> int:
    if not path.exists():
        return 0
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute(query).fetchone()[0])
    finally:
        connection.close()


def probe_fts(index: LanceDBSearchIndex, terms: tuple[str, ...]) -> dict[str, int]:
    """每个探针词在 FTS 通道能召回多少条行（只读，不做任何发布）。"""
    out: dict[str, int] = {}
    for term in terms:
        try:
            out[term] = len(index.search_fts(term, CandidateFilters(), PROBE_LIMIT))
        except Exception as error:  # 探针失败不该阻断重建
            out[term] = -1
            print(f"  probe {term} 失败：{type(error).__name__}")
    return out


def scan_location_terms(root: Path) -> dict:
    """抽查索引父行 location_terms 的归一化落地情况。

    注意：``LanceDBSearchIndex._record`` 会把原始 ``location`` 也并进 ``location_terms``
    （``["通辽", "内蒙古通辽"]``），所以判定标准是「**归一化后的城市是否已在列里**」，
    而不是「列里只有归一化结果」——后者永远不成立。

    ``rewritten``：``location`` 原值需要归一化的行数（「上海市」「内蒙古通辽」这类）；
    ``stale``：需要归一化但列里没有规范城市名的行数 —— 这才是「没生效」的直接证据；
    ``landed``：列里已含全部规范城市名的行数。
    """
    import lancedb

    table = lancedb.connect(str(root)).open_table("candidate_chunks")
    columns = ["candidate_id", "chunk_type", "location", "location_terms",
               "keyword_index_text"]
    try:
        arrow = table.to_lance().to_table(columns=columns)
    except Exception:
        # 未装 pylance 时退回 pyarrow 全表读取再裁列。
        arrow = table.to_arrow()
        arrow = arrow.drop([c for c in arrow.column_names if c not in columns])
    data = {name: arrow.column(name).to_pylist() for name in arrow.column_names}
    total = rewritten = landed = stale = unrecognized = 0
    token_rows = {term: 0 for term in ALIAS_PAIRS}
    alias_co = {pair: 0 for pair in ALIAS_COOCCURRENCE}
    stale_candidates: list[str] = []
    samples: list[dict] = []
    for i in range(arrow.num_rows):
        if data["chunk_type"][i] != "parent":
            continue
        terms_in_text = set(str(data["keyword_index_text"][i] or "").split())
        for term in ALIAS_PAIRS:
            if term in terms_in_text:
                token_rows[term] += 1
        for left, right in ALIAS_COOCCURRENCE:
            if left in terms_in_text and right in terms_in_text:
                alias_co[(left, right)] += 1
        raw = data["location"][i]
        if not raw:
            continue
        total += 1
        expected = set(normalize_location_terms(str(raw)))
        terms = {str(term) for term in (data["location_terms"][i] or [])}
        needs_rewrite = bool(expected) and expected != {str(raw)}
        if needs_rewrite:
            rewritten += 1
        elif expected == {str(raw)}:
            unrecognized += 1
        if expected and expected <= terms:
            landed += 1
        if needs_rewrite and not expected <= terms:
            stale += 1
            candidate_id = str(data["candidate_id"][i])
            if candidate_id not in stale_candidates:
                stale_candidates.append(candidate_id)
            if len(samples) < 12:
                samples.append({"raw": str(raw), "terms": sorted(terms), "expected": sorted(expected)})
    return {
        "parent_rows": total,
        "rewritten": rewritten,
        "landed": landed,
        "stale": stale,
        "stale_candidates": stale_candidates,
        "unrecognized": unrecognized,
        "alias_token_rows": token_rows,
        "alias_cooccurrence": {f"{left}+{right}": count
                               for (left, right), count in alias_co.items()},
        "samples": samples,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--output", default=str(ROOT / ".tmp-plan" / "rebuild-2026-09-21.json"))
    parser.add_argument("--drain-timeout", type=float, default=3600.0)
    parser.add_argument("--stall-cycles", type=int, default=6)
    parser.add_argument("--verify-only", action="store_true",
                        help="只读校验：不装配 runtime、不写任何东西，仅重新扫描索引状态")
    parser.add_argument("--reproject-inconsistent", action="store_true",
                        help="把 location_terms 与 location 列不一致的候选人单独重投一遍")
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    paths = AppPaths.from_root(data_root)
    settings = build_settings(RuntimeArgs(host="127.0.0.1", port=1, token="0" * 64, data_root=data_root))
    embedding_model, dimension = embedding_identity(settings)
    database = paths.database

    print(f"data_root={data_root}")
    print(f"期望版本 schema={INDEX_SCHEMA_VERSION} chunk={INDEX_CHUNK_VERSION} "
          f"embedding={embedding_model} dim={dimension}")
    before_metadata = read_metadata(paths.search)
    print(f"重建前 metadata={before_metadata}")
    upgrade = plan_index_upgrade(paths.search, embedding_model=embedding_model, vector_dimension=dimension)
    print(f"升级计划={'None（已是当前版本）' if upgrade is None else f'{upgrade.mode}: {upgrade.reason}'}")

    before_counts = {
        "candidates": count_rows(database, "SELECT COUNT(*) FROM candidate"),
        "jds": count_rows(database, "SELECT COUNT(*) FROM jd"),
        "outbox_pending": count_rows(
            database,
            "SELECT COUNT(*) FROM index_sync WHERE requested_version > applied_version"),
    }
    print(f"重建前计数={before_counts}")

    index = LanceDBSearchIndex(paths.search, vector_dimension=dimension, embedding_model=embedding_model)
    before_probes = probe_fts(index, PROBES)
    before_locations = scan_location_terms(paths.search)
    print(f"重建前 FTS 探针={before_probes}")
    print(f"重建前 location_terms 抽查={ {k: v for k, v in before_locations.items() if k != 'samples'} }")
    # 释放预扫描留下的连接句柄，避免与就地升级（补列）抢同一张表。
    del index
    gc.collect()

    # 只读校验模式：不装配 runtime、不写任何东西，仅重新扫描索引状态。
    if args.verify_only:
        verify_index = LanceDBSearchIndex(paths.search, vector_dimension=dimension,
                                          embedding_model=embedding_model)
        await _emit_report(
            args=args, paths=paths, database=database, data_root=data_root,
            dimension=dimension, embedding_model=embedding_model,
            upgrade=None, before_metadata=before_metadata,
            after_upgrade_metadata=read_metadata(paths.search),
            before_counts=before_counts, before_probes=before_probes,
            before_locations=before_locations,
            after_probes=probe_fts(verify_index, PROBES),
            after_locations=scan_location_terms(paths.search),
            final_status={"pending": before_counts["outbox_pending"], "failed": 0, "items": []},
            timing={"boot": 0.0, "cycles": 0, "total": 0.0, "fts_optimize": 0.0},
            verify_only=True,
        )
        return

    started = time.monotonic()
    runtime = build_runtime(settings)
    boot_seconds = time.monotonic() - started
    sync = runtime.services.index_sync_service
    status = sync.status()
    print(f"装配完成（{boot_seconds:.1f}s）待投影={status['pending']} 失败={status['failed']} "
          f"重建跟踪={status['rebuild']}")
    after_upgrade_metadata = read_metadata(paths.search)
    print(f"升级后 metadata={after_upgrade_metadata}")

    cycles = 0
    stalls = 0
    # 定点补投：只把这些候选人重新入队，不动其余实体（用于修正 location_terms 与
    # location 列不同源这类脏行）。
    if args.reproject_inconsistent and before_locations["stale_candidates"]:
        targets = before_locations["stale_candidates"]
        with runtime.services.session_factory() as session, session.begin():
            for candidate_id in targets:
                enqueue_sync(session, "candidate", candidate_id)
        print(f"定点重投 {len(targets)} 个 location 不一致的候选人")
        status = sync.status()
    previous_pending = status["pending"]
    try:
        while True:
            if status["pending"] == 0:
                break
            if time.monotonic() - started > args.drain_timeout:
                print("排空超时，停止等待（未完成的实体仍在队列里，可重跑本脚本续跑）")
                break
            cycle_started = time.monotonic()
            applied = await sync.run_once(batch_size=args.batch)
            cycles += 1
            status = sync.status()
            elapsed = time.monotonic() - cycle_started
            print(f"  cycle={cycles} applied={applied} pending={status['pending']} "
                  f"failed={status['failed']} cycle={elapsed:.1f}s "
                  f"总耗时={time.monotonic() - started:.0f}s", flush=True)
            if status["pending"] >= previous_pending and applied == 0:
                stalls += 1
                if stalls >= args.stall_cycles:
                    print("连续多轮无进展，停止；剩余项原因：")
                    for item in status["items"][:10]:
                        print(f"    {item}")
                    break
            else:
                stalls = 0
            previous_pending = status["pending"]

        # 重投影后 FTS 索引需要重建（写入会置 .fts-dirty）；不清干净，新别名与归一化都搜不到。
        optimize_started = time.monotonic()
        await asyncio.to_thread(runtime.services.search_service.optimize_pending)
        optimize_seconds = time.monotonic() - optimize_started

        final_status = sync.status()
        # 预扫描时释放过句柄，这里重新开一个只读实例做收尾探针。
        after_index = LanceDBSearchIndex(paths.search, vector_dimension=dimension,
                                         embedding_model=embedding_model)
        after_probes = probe_fts(after_index, PROBES)
        after_locations = scan_location_terms(paths.search)
        after_counts = {
            "candidates": count_rows(database, "SELECT COUNT(*) FROM candidate"),
            "jds": count_rows(database, "SELECT COUNT(*) FROM jd"),
            "outbox_pending": count_rows(
                database,
                "SELECT COUNT(*) FROM index_sync WHERE requested_version > applied_version"),
        }

        await _emit_report(
            args=args, paths=paths, database=database, data_root=data_root,
            dimension=dimension, embedding_model=embedding_model,
            upgrade=upgrade, before_metadata=before_metadata,
            after_upgrade_metadata=after_upgrade_metadata,
            before_counts=before_counts, before_probes=before_probes,
            before_locations=before_locations,
            after_probes=after_probes, after_locations=after_locations,
            final_status=final_status,
            timing={"boot": boot_seconds, "cycles": cycles,
                    "total": time.monotonic() - started, "fts_optimize": optimize_seconds},
            verify_only=False,
        )
    finally:
        if runtime.providers.http_client is not None:
            await runtime.providers.http_client.aclose()
        if runtime.services.ai_manager is not None:
            await runtime.services.ai_manager.close()


async def _emit_report(*, args, paths, database, data_root, dimension, embedding_model,
                      upgrade, before_metadata, after_upgrade_metadata, before_counts,
                      before_probes, before_locations, after_probes, after_locations,
                      final_status, timing, verify_only: bool) -> None:
    """写报告并打印人类可读摘要（两条路径共用，口径一致）。"""
    def sanitized(scan: dict) -> dict:
        # 报告里候选人一律用 sha256 别名，不落原始 ID。
        return {**scan, "stale_candidates": [alias(cid) for cid in scan.get("stale_candidates", [])]}

    after_counts = {
        "candidates": count_rows(database, "SELECT COUNT(*) FROM candidate"),
        "jds": count_rows(database, "SELECT COUNT(*) FROM jd"),
        "outbox_pending": count_rows(
            database,
            "SELECT COUNT(*) FROM index_sync WHERE requested_version > applied_version"),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verify_only": verify_only,
        "data_root": str(data_root),
        "expected_version": {"schema": INDEX_SCHEMA_VERSION, "chunk": INDEX_CHUNK_VERSION,
                             "embedding": embedding_model, "dimension": dimension},
        "upgrade": {"mode": upgrade.mode if upgrade else None,
                    "reason": upgrade.reason if upgrade else None,
                    "metadata_before": before_metadata,
                    "metadata_after_upgrade": after_upgrade_metadata},
        "counts": {"before": before_counts, "after": after_counts},
        "pending": {"after": final_status["pending"], "failed": final_status["failed"],
                    "items": final_status["items"][:20]},
        "timing_seconds": {key: round(value, 1) for key, value in timing.items()},
        "fts_probes": {"before": before_probes, "after": after_probes},
        "location_terms": {"before": sanitized(before_locations),
                           "after": sanitized(after_locations)},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 重建结果 ===")
    print(f"版本：{after_upgrade_metadata}")
    print(f"耗时：装配 {timing['boot']:.1f}s + {timing['cycles']} 轮同步 + "
          f"FTS 优化 {timing['fts_optimize']:.0f}s = {timing['total']:.0f}s")
    print(f"计数：前={before_counts} 后={after_counts}")
    print(f"队列：待投影={final_status['pending']} 失败={final_status['failed']}")
    print(f"FTS 探针：前={before_probes}")
    print(f"          后={after_probes}")
    print(f"location_terms：需归一化={after_locations['rewritten']}行 "
          f"已落地={after_locations['landed']}行 仍未归一化={after_locations['stale']}行 "
          f"认不出={after_locations['unrecognized']}行")
    print(f"              （前 需归一化={before_locations['rewritten']}行 "
          f"已落地={before_locations['landed']}行 仍未归一化={before_locations['stale']}行）")
    print(f"别名同现行数（文档侧展开是否落地）：{after_locations['alias_cooccurrence']}")
    if after_locations["stale"]:
        print(f"仍未归一化样例：{after_locations['samples'][:3]}")
    print(f"报告已写入 {output}")


if __name__ == "__main__":
    asyncio.run(main())
