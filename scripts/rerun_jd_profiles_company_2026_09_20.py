"""重跑「点名具体公司」的 JD 画像，让模型产出 company_history 硬条件。

只处理画像/要求文本里出现具体公司名（英文名优先，规则抽取覆盖不到）的 JD：
调用运行中的后端 `POST /api/jd/{jd_id}/regen-profile` 生成画像与约束，
再把画像双形态 + **合并后的约束**（已有规则兜底结果 + 模型输出，去重）写回
`PUT /api/jd/{jd_id}/parsed`。写回会触发索引异步重建。

用法：python scripts/rerun_jd_profiles_company_2026_09_20.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev-data"
BASE = "http://127.0.0.1:43127"
TOKEN = "0" * 64
HEADERS = {"X-Kerui-Session": TOKEN}
# 目标：画像文本里**点名了具体公司**的 JD（中英文都算）。
COMPANY_MARKERS = ("Google", "谷歌", "微软", "Microsoft", "Oracle", "甲骨文",
                   "Amazon", "亚马逊", "字节", "阿里", "腾讯", "美团", "京东", "华为")


def constraint_key(item: dict) -> tuple:
    return (str(item.get("kind")), str(item.get("strength")).upper(),
            tuple(str(a) for a in (item.get("alternatives") or ())))


def target_jds() -> list[tuple[str, str, list[dict]]]:
    con = sqlite3.connect(f"file:{ROOT}/.dev-data/db/recruit.sqlite3?mode=ro", uri=True)
    rows = []
    for jd_id, title, raw in con.execute(
        """select j.id, j.title, rev.parsed_data from jd j
           join jd_revision rev on rev.jd_id=j.id and rev.is_current=1
           where j.deleted_at is null and rev.status='READY'"""
    ):
        data = json.loads(raw)
        text = " ".join(str(x) for x in (
            data.get("candidate_profile") or "", data.get("summary") or "",
            " ".join(str(d) for d in (data.get("core_duties") or [])),
            " ".join(str((r or {}).get("value") or "") for r in (data.get("requirements") or [])),
        ))
        if any(marker in text for marker in COMPANY_MARKERS):
            rows.append((jd_id, title, [c for c in (data.get("exact_constraints") or []) if isinstance(c, dict)]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只列出目标 JD，不调用模型")
    args = parser.parse_args()

    targets = target_jds()
    print(f"命中「点名公司」的 JD = {len(targets)} 个")
    for jd_id, title, existing in targets:
        print(f"  {title}  现有约束 {len(existing)} 条")
    if args.dry_run:
        return

    for jd_id, title, existing in targets:
        print(f"\n=== 《{title}》 ===")
        with httpx.Client(timeout=300, headers=HEADERS) as client:
            response = client.post(f"{BASE}/api/jd/{jd_id}/regen-profile", json={})
            if response.status_code != 200:
                print(f"  生成失败：{response.status_code} {response.text[:200]}")
                continue
            generated = response.json()
            model_constraints = [c for c in (generated.get("constraints") or []) if isinstance(c, dict)]

            merged, seen = [], set()
            for item in [*existing, *model_constraints]:
                key = constraint_key(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)

            payload = {"parsed_data": {
                "candidate_profile": generated.get("summary") or "",
                "candidate_profile_points": generated.get("points") or [],
                "candidate_profile_compact": generated.get("compact") or "",
                "exact_constraints": merged,
            }}
            saved = client.put(f"{BASE}/api/jd/{jd_id}/parsed", json=payload)
            print(f"  模型约束 {len(model_constraints)} 条："
                  f"{[(c.get('kind'), c.get('strength'), c.get('alternatives')) for c in model_constraints]}")
            print(f"  合并后写回 {len(merged)} 条 → HTTP {saved.status_code}")
            if saved.status_code != 200:
                print(f"  {saved.text[:200]}")


if __name__ == "__main__":
    main()
