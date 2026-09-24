"""画像文本里的「总工作年限」「年龄」就地替换。

画像里的年限与年龄都是**生成当时的快照**，不会自己随时间变。整段重生需要调用模型，
成本高、会覆盖使用者的措辞，而且首日会命中一大批候选人。所以这里只把这两个数字改掉，
其余文字一个字都不动。

只认两类位置（按优先级）：

1. 「N年…经验」——画像规范要求的总年限标准写法（见 ``providers/profile_spec``）；
2. 找不到该句式时，**全文只有一个「N年」且它等于本次重算前的年限**时替换它。
   历史画像里也有只写「4年」不带「经验」的写法。

**刻意不碰**：``2024年`` 这类年份，以及「近4年先后任职于…」「3年架构经验」这类
单段经历 / 子技能年限——它们不是总年限，改了就是改错。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# 总年限的标准写法：数字 + 年 + 至多 25 个非标点字符 + 经验。
_EXPERIENCE_YEARS = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(年)([^。；，,、\n]{0,25}?)(经验)")
# 任意「N年」，用负向断言排除 4 位年份（2024年）。
_ANY_YEARS = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*年")
# 年龄：19岁 / 27 岁。
_AGE = re.compile(r"(?<!\d)(\d{1,3})\s*岁")

_PROFILE_TEXT_FIELDS = ("ai_profile_summary", "ai_profile_narrative")


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def _display_years(total_years) -> str | None:
    """画像里写整数年限，与界面列表的展示口径保持一致（``Math.floor``）。"""
    value = _decimal(total_years)
    if value is None:
        return None
    return str(int(value))


def _replace_experience_years(text: str, target: str) -> str:
    return _EXPERIENCE_YEARS.sub(
        lambda match: f"{target}{match.group(2)}{match.group(3)}{match.group(4)}", text
    )


def _replace_sole_years(text: str, target: str, accepted: set[Decimal]) -> str:
    """仅当全文只有一个「N年」且其数值等于重算前的年限时才替换。"""
    matches = list(_ANY_YEARS.finditer(text))
    if len(matches) != 1:
        return text
    value = _decimal(matches[0].group(1))
    if value is None or value not in accepted:
        return text
    match = matches[0]
    return f"{text[:match.start()]}{target}年{text[match.end():]}"


def refresh_profile_text(
    text: str,
    *,
    total_years=None,
    age=None,
    previous_total_years=None,
) -> str | None:
    """改掉文本里的总年限与年龄；返回 ``None`` 表示无改动（调用方不要写库）。

    ``previous_total_years`` 是**本次重算之前**的年限，用于第 2 类兜底匹配——画像里的
    数字是上一轮写进去的，拿新值去比是比不上的。
    """
    if not text:
        return None
    updated = text

    target_years = _display_years(total_years)
    if target_years is not None and _EXPERIENCE_YEARS.search(updated):
        updated = _replace_experience_years(updated, target_years)
    elif target_years is not None:
        previous = _decimal(previous_total_years)
        accepted = {value for value in (previous, _decimal(int(previous)) if previous is not None else None)
                    if value is not None}
        accepted.add(_decimal(target_years))
        updated = _replace_sole_years(updated, target_years, accepted)

    if age is not None:
        updated = _AGE.sub(lambda match: f"{int(age)}岁", updated)

    return updated if updated != text else None


def refresh_profile_fields(parsed_data: dict, *, total_years=None, previous_total_years=None) -> bool:
    """就地刷新 ``parsed_data`` 里的画像字段；返回是否发生了改动。

    覆盖双形态的三个字段（整体段落 / 叙述 / 分点），避免同一份画像里新旧数字并存。
    """
    age = parsed_data.get("age")
    changed = False
    for field in _PROFILE_TEXT_FIELDS:
        text = parsed_data.get(field)
        if not isinstance(text, str):
            continue
        updated = refresh_profile_text(
            text,
            total_years=total_years,
            age=age,
            previous_total_years=previous_total_years,
        )
        if updated is not None:
            parsed_data[field] = updated
            changed = True

    points = parsed_data.get("ai_profile_points")
    if isinstance(points, list):
        refreshed = []
        for point in points:
            if isinstance(point, str):
                updated = refresh_profile_text(
                    point, total_years=total_years, age=age,
                    previous_total_years=previous_total_years,
                )
                refreshed.append(updated if updated is not None else point)
                changed = changed or updated is not None
            elif isinstance(point, dict) and isinstance(point.get("text"), str):
                updated = refresh_profile_text(
                    point["text"], total_years=total_years, age=age,
                    previous_total_years=previous_total_years,
                )
                refreshed.append({**point, "text": updated} if updated is not None else point)
                changed = changed or updated is not None
            else:
                refreshed.append(point)
        if changed:
            parsed_data["ai_profile_points"] = refreshed
    return changed
