"""工作年限的确定性计算（连续口径）。

解析提示词里由模型估算的 ``total_years`` 只是「解析那一刻」的一次性快照，不会随
时间增长；本模块提供可复现的代码口径，供每日滚动刷新使用：

    总工作年限 = 最早一段工作的起始月 → 当前月（连续口径，含中间空窗）

只在无法从时间线确定时返回 ``None``，由调用方保留原值兜底。

日期解析与 ``direction.classifier`` 共用同一份实现，避免两套口径漂移。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_DATE_RE = re.compile(r"(\d{4})\s*[.\-/年]?\s*(\d{1,2})?")

# 与 ParsedResume / 纠正校验 / 前端输入框一致的上限。
_MAX_YEARS = Decimal("80")


def parse_month(value: str) -> int | None:
    """把 ``2020.07`` / ``2020-7`` / ``2020年7月`` / ``2020`` 解析为年*12+月的序数。"""
    match = _DATE_RE.search(value or "")
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2) or 1) - 1


def _start_date_text(experience) -> str:
    """兼容 ``parsed_data`` 里的 dict 与 ``NormalizedExperience`` 对象。"""
    if isinstance(experience, dict):
        return str(experience.get("start_date") or "")
    return str(getattr(experience, "start_date", "") or "")


def compute_total_years(experiences, today: date) -> Decimal | None:
    """连续口径总工作年限，量化到 0.1（与 DB ``NUMERIC(5,1)`` 一致）。

    无法确定时返回 ``None``：没有经历、所有 ``start_date`` 都解析不出、
    或解析出的起始月晚于当前月（脏数据）。
    """
    starts = [parse_month(_start_date_text(item)) for item in (experiences or [])]
    parsed = [value for value in starts if value is not None]
    if not parsed:
        return None
    current = today.year * 12 + today.month - 1
    earliest = min(parsed)
    if earliest > current:
        return None
    years = (Decimal(current - earliest) / Decimal(12)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return min(years, _MAX_YEARS)
