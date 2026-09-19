from __future__ import annotations

import re

# 学历层级：低 -> 高。用于「本科及以上」这类包含式条件。
DEGREE_ORDER = ("ASSOCIATE", "BACHELOR", "MASTER", "DOCTORATE")


def _key(value: str) -> str:
    """Normalize a degree token for lookup: lowercase, drop spaces/underscores."""
    return re.sub(r"[_\s]+", "", value.casefold())


# 学历别名 -> 规范值。覆盖旧前端、历史数据和中文自然语言中的各种写法。
DEGREE_ALIASES: dict[str, str] = {
    # 大专
    "associate": "ASSOCIATE",
    "college": "ASSOCIATE",
    "junior college": "ASSOCIATE",
    "大专": "ASSOCIATE",
    "专科": "ASSOCIATE",
    # 本科
    "bachelor": "BACHELOR",
    "本科": "BACHELOR",
    "学士": "BACHELOR",
    # 硕士
    "master": "MASTER",
    "硕士": "MASTER",
    # 博士
    "doctorate": "DOCTORATE",
    "doctor": "DOCTORATE",
    "phd": "DOCTORATE",
    "博士": "DOCTORATE",
}

_DEGREE_ALIAS_BY_KEY = {
    _key(alias): canonical for alias, canonical in DEGREE_ALIASES.items()
}


def normalize_degree(value: str | None) -> str | None:
    """Map any degree spelling to the canonical English value.

    Returns ``None`` for empty/unknown values so callers can decide their own
    fallback (e.g. keep the raw parsed value).
    """
    if not value:
        return None
    cleaned = " ".join(str(value).split()).strip()
    if not cleaned:
        return None

    key = _key(cleaned)
    # 「本科及以上」这类带后缀的最低学历表达，先去掉后缀再映射。
    for suffix in ("及以上", "以上", "或以上"):
        suffix_key = _key(suffix)
        if key.endswith(suffix_key):
            key = key[: -len(suffix_key)]
            break

    if key in _DEGREE_ALIAS_BY_KEY:
        return _DEGREE_ALIAS_BY_KEY[key]

    upper = cleaned.upper()
    if upper in DEGREE_ORDER:
        return upper
    return None


def degrees_at_least(degree: str | None) -> tuple[str, ...]:
    """Return the canonical degrees that satisfy a minimum-degree condition."""
    if degree is None:
        return ()
    normalized = normalize_degree(degree)
    if normalized is None:
        return (degree,)
    idx = DEGREE_ORDER.index(normalized)
    return tuple(DEGREE_ORDER[idx:])
