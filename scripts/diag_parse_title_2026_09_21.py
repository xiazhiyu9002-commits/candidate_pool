"""诊断：为什么解析产出的 title 条件会把结果清空（2026-09-21 抽检发现的头号漏召原因）。

结论先行：``title`` 在索引侧是 **整串子串匹配**（``title_text LIKE '%<值>%'``），
而 LLM 把 JD 里的**长职位短语**当职位名下推，两者几乎不可能逐字重合 → 交集成空。

用法：

    py -3.12 scripts/diag_parse_title_2026_09_21.py \
        --report .tmp-plan/parse-quality-real.json --data-root .dev-data

输出每个「空结果查询」的 title 值与三种口径下的命中数：
- ``full``：职位文本包含**整串**（生产实际口径）→ 预期 ~0；
- ``tokens``：把整串按标点/斜杠拆开，各片段分别命中的候选人数 → 说明「词都在，整串不在」；
- ``last_noun``：取最后一个「…师/…经理/…工程师/…负责人」结尾的片段单独命中数。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))


def load_title_texts(root: Path) -> list[str]:
    """父行的职位词项文本（索引侧 LIKE 匹配的目标列）。"""
    import lancedb

    table = lancedb.connect(str(root)).open_table("candidate_chunks")
    columns = ["chunk_type", "title_text", "title_terms"]
    try:
        arrow = table.to_lance().to_table(columns=columns)
    except Exception:
        arrow = table.to_arrow()
        arrow = arrow.drop([c for c in arrow.column_names if c not in columns])
    kinds = arrow.column("chunk_type").to_pylist()
    texts = arrow.column("title_text").to_pylist()
    terms = arrow.column("title_terms").to_pylist()
    result: list[str] = []
    for i, kind in enumerate(kinds):
        if kind != "parent":
            continue
        text = texts[i] or " ".join(str(t) for t in (terms[i] or []))
        if text:
            result.append(str(text).casefold())
    return result


def _split(value: str) -> list[str]:
    parts = re.split(r"[/、,，|（）()\s]+", value)
    return [part.strip() for part in parts if len(part.strip()) >= 2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=str(ROOT / ".tmp-plan" / "parse-quality-real.json"))
    parser.add_argument("--data-root", default=str(ROOT / ".dev-data"))
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    attribution = report.get("zero_result_attribution") or []
    if not attribution:
        raise SystemExit("报告里没有 zero_result_attribution，先跑 --recount 生成")

    texts = load_title_texts(Path(args.data_root).expanduser().resolve() / "search")
    print(f"有职位文本的候选人父行={len(texts)}")
    print(f"{'查询':10} {'整串命中':>8} {'片段命中':>28}   title 值")
    for item in attribution:
        value = (item.get("conditions") or {}).get("title")
        if not value:
            continue
        folded = str(value).strip().casefold()
        full = sum(1 for text in texts if folded in text)
        tokens = [(part, sum(1 for text in texts if part.casefold() in text))
                  for part in _split(str(value))]
        tokens.sort(key=lambda pair: -pair[1])
        print(f"{item['qid']:10} {full:>8} "
              f"{'; '.join(f'{name}={count}' for name, count in tokens[:4]):>28}   {value}")


if __name__ == "__main__":
    main()
