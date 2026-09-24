"""只读：业务方向（business_directions）在候选人与 JD 上的覆盖率与取值分布。

用途：判断「把业务方向加进匹配初筛」是否值得 —— 若覆盖率低或取值过于分散，
加进去只会徒增空结果风险。
"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

DB = (Path(__file__).resolve().parents[1] / ".dev-data" / "db" / "recruit.sqlite3").as_posix()


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    cand_rows = con.execute(
        "select parsed_data from resume_revision where is_current=1 and status='READY'"
    ).fetchall()
    jd_rows = con.execute(
        "select parsed_data from jd_revision where is_current=1 and status='READY'"
    ).fetchall()

    def census(rows, label):
        total = len(rows)
        with_value = 0
        counter: collections.Counter = collections.Counter()
        empty_like = 0
        for (raw,) in rows:
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            values = [str(v) for v in (data.get("business_directions") or []) if v]
            if values:
                with_value += 1
                counter.update(values)
            else:
                empty_like += 1
        print(f"\n{label}：共 {total} 条，有业务方向 {with_value} "
              f"({with_value / total * 100:.1f}%)，空 {empty_like}")
        print(f"  取值种类={len(counter)}  分布前 12：{counter.most_common(12)}")
        return counter

    census(cand_rows, "候选人简历（当前 READY）")
    census(jd_rows, "岗位 JD（当前 READY）")


if __name__ == "__main__":
    main()
