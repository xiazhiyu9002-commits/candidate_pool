"""只读核查：索引真实规模（行数 / 唯一候选人 / chunk 类型分布）。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import sqlite3  # noqa: E402

from kerui_recruit.search.lancedb_index import LanceDBSearchIndex  # noqa: E402


def main() -> None:
    settings = json.loads((ROOT / ".dev-data" / "config" / "settings.json").read_text(encoding="utf-8"))
    index = LanceDBSearchIndex(ROOT / ".dev-data" / "search", vector_dimension=1024,
                               embedding_model=settings["siliconflow_embedding_model"])
    table = index.database.open_table(index.table_name)
    print("索引总行数(chunks):", table.count_rows())
    arrow = table.search().select(["candidate_id", "chunk_type"]).limit(None).to_arrow()
    candidates = arrow.column("candidate_id").to_pylist()
    chunk_types = arrow.column("chunk_type").to_pylist()
    print("取回行数:", len(candidates), "唯一候选人:", len(set(candidates)))
    print("chunk_type 分布:", dict(Counter(chunk_types)))

    db = ROOT / ".dev-data" / "db" / "recruit.sqlite3"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    total = con.execute("select count(*) from candidate").fetchone()[0]
    available = con.execute("select count(*) from candidate where status='AVAILABLE' and deleted_at is null").fetchone()[0]
    revisions = con.execute("select count(*) from resume_revision where is_current=1 and status='READY'").fetchone()[0]
    print(f"\n数据库: candidate 总数={total}  AVAILABLE且未删={available}  当前 READY 简历修订={revisions}")


if __name__ == "__main__":
    main()
