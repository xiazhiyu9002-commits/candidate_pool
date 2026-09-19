from __future__ import annotations

import importlib.resources
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VendorPreset:
    key: str
    label: str
    base_url: str
    text_model: str = ""
    vision_model: str = ""
    embedding_model: str = ""
    rerank_model: str = ""
    deprecated: bool = False


# 旧设置页「模型 API 配置」的兼容投影：由新的生成式 AI 内置目录派生。
# 仅保留七个生成式入口，不再保留 MiniMax 与旧模型 ID；Embedding/Rerank 属搜索能力，
# 不在此投影内（保持空字符串）。
def _legacy_vendors() -> dict[str, VendorPreset]:
    raw = (
        importlib.resources.files("kerui_recruit.providers.ai")
        .joinpath("provider_catalog.builtin.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(raw)
    vendors: dict[str, VendorPreset] = {}
    for provider_id, preset in data["providers"].items():
        recommended = preset.get("recommended_models") or {}
        vendors[provider_id] = VendorPreset(
            key=provider_id,
            label=preset["label"],
            base_url=preset["base_url"],
            text_model=recommended.get("fast_text", ""),
            vision_model=recommended.get("vision", ""),
            embedding_model="",
            rerank_model="",
            deprecated=True,
        )
    return vendors


VENDORS: dict[str, VendorPreset] = _legacy_vendors()
