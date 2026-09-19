"""双通道召回：同向/相邻方向 + 救援通道，合并去重。

同向通道命中者优先进入候选池；救援通道补足不重复的「跨方向/方向待核」者，
避免在召回前因方向等值过滤漏掉直接相关人选。救援位是召回预算，不是评分加成。
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


def merge_recall_lanes(
    same_hits: list[T],
    rescue_hits: list[T],
    *,
    key: Callable[[T], str],
    limit: int = 100,
    rescue_slots: int = 30,
) -> list[T]:
    """按 ``key`` 去重合并：同向通道优先，救援通道补足至 ``rescue_slots`` 个不重复者。"""
    seen: set[str] = set()
    merged: list[T] = []
    for hit in same_hits:
        k = key(hit)
        if k not in seen:
            seen.add(k)
            merged.append(hit)
    added_rescue = 0
    for hit in rescue_hits:
        k = key(hit)
        if k not in seen and added_rescue < rescue_slots:
            seen.add(k)
            merged.append(hit)
            added_rescue += 1
    return merged[:limit]
