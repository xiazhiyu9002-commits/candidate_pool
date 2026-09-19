"""在 52 张方向盲卡上验证 v2 确定性分类器与模型标签的一致性。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from kerui_recruit.direction.classifier import classify_direction

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / ".semantic-audit-snapshot"

cards = json.loads((SNAP / "direction_blind_cards.json").read_text(encoding="utf-8"))
labels = json.loads((SNAP / "direction_blind_labels.json").read_text(encoding="utf-8"))["labels"]


def to_data(card: dict) -> dict:
    return {
        "skills": card.get("核心技能") or [],
        "summary": card.get("画像") or "",
        "experiences": [{"summary": d} for d in (card.get("近期职责") or [])],
        "projects": [{"summary": p} for p in (card.get("项目证据") or [])],
    }


consistent_model = 0
consistent_stored = 0
diffs_model = []
for card in cards:
    cid = card["id"]
    stored = card["stored"]
    decision = classify_direction(to_data(card["card"]))
    model_dir = labels.get(cid, {}).get("direction")
    if decision.direction == model_dir:
        consistent_model += 1
    if decision.direction == stored:
        consistent_stored += 1
    else:
        diffs_model.append((cid, stored, decision.direction, decision.confidence))

print(f"v2 分类器 vs 模型标签 一致: {consistent_model}/{len(cards)}")
print(f"v2 分类器 vs 库内旧标签 一致: {consistent_stored}/{len(cards)}")
print("\n与库内旧标签不一致的卡片（旧标签 -> v2 主方向 [置信]）：")
for cid, stored, new, conf in diffs_model:
    print(f"  {cid}: {stored} -> {new} ({conf})")
