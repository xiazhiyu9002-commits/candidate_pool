"""schema 版本常量自身的守卫。

`SCHEMA_VERSION` 与 `DEFAULT_UPGRADES` 是**两处独立定义**：`migrate` 的
`while version < target_version` 循环以常量为上界，而加列靠 Upgrades 里的函数。
只加 Upgrade 忘了改常量时，循环不执行、新列不加，存量库启动才报「no such column」，
是一类静默失败。这里把它变成构建期可见的失败。
"""
from __future__ import annotations

from kerui_recruit.db.migrate import SCHEMA_VERSION
from kerui_recruit.db.upgrades import DEFAULT_UPGRADES


def test_schema_version_matches_last_upgrade() -> None:
    assert SCHEMA_VERSION == DEFAULT_UPGRADES[-1].to_version


def test_upgrades_are_contiguous_from_v1() -> None:
    assert DEFAULT_UPGRADES[0].from_version == 1
    for previous, current in zip(DEFAULT_UPGRADES, DEFAULT_UPGRADES[1:]):
        assert current.from_version == previous.to_version
