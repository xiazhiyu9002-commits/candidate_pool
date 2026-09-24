"""城市词典单元测试：查询侧解析与索引侧 location_terms 归一化必须同源同口径。"""
from kerui_recruit.search.cities import (
    CITY_GROUPS,
    CITY_NAMES,
    canonical_city,
    normalize_location_terms,
)


def test_city_names_are_canonical_and_unique():
    """规范形式是纯城市名：不带「市」后缀、无重复，否则精确 array_has_any 会错配。"""
    assert len(CITY_NAMES) == len(set(CITY_NAMES))
    assert not [name for name in CITY_NAMES if name.endswith("市")]
    for common in ("北京", "上海", "深圳", "广州", "杭州", "成都", "苏州", "西安", "乌鲁木齐", "拉萨", "香港"):
        assert common in CITY_NAMES


def test_city_groups_only_reference_known_cities():
    for group, cities in CITY_GROUPS.items():
        assert cities, group
        assert set(cities) <= set(CITY_NAMES)


def test_canonical_city_strips_province_prefix_and_city_suffix():
    assert canonical_city("广东省深圳市") == "深圳"
    assert canonical_city("上海市") == "上海"
    assert canonical_city("四川省成都市") == "成都"
    assert canonical_city("江苏省无锡市") == "无锡"
    assert canonical_city("北京市海淀区") == "北京"
    assert canonical_city("杭州") == "杭州"


def test_canonical_city_rejects_ambiguous_district_only_forms():
    """「朝阳区」多半指北京，但朝阳也是辽宁地级市，语义不确定时不做映射。"""
    assert canonical_city("朝阳区") is None
    assert canonical_city("浦东新区") is None
    assert canonical_city("") is None


def test_normalize_location_terms_rewrites_non_canonical_forms():
    assert normalize_location_terms("广东省深圳市") == ("深圳",)
    assert normalize_location_terms("上海市") == ("上海",)
    assert normalize_location_terms("上海-浦东新区") == ("上海",)
    assert normalize_location_terms("成都/杭州") == ("成都", "杭州")
    assert normalize_location_terms("江苏省无锡市") == ("无锡",)


def test_normalize_location_terms_keeps_unrecognized_values():
    """认不出时宁可保留原值：绝不把值弄空，海外城市也不因空格被切碎。"""
    assert normalize_location_terms("远程") == ("远程",)
    assert normalize_location_terms("Singapore") == ("Singapore",)
    assert normalize_location_terms("New York") == ("New York",)
    assert normalize_location_terms("") == ()
    assert normalize_location_terms(None) == ()


def test_normalize_location_terms_expands_city_groups():
    assert normalize_location_terms("北上广深") == ("北京", "上海", "广州", "深圳")
