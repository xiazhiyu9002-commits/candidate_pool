"""城市词典：查询侧地点解析与索引侧 ``location_terms`` 归一化共用同一份数据与同一套规则。

实测（2026-09-21）：现网 1,475 条 ``location`` 共 172 种写法，98% 已是规范城市名，
但有 32 条是非规范写法（``上海市``、``广东省深圳市``、``成都/杭州``、``上海-浦东新区``）。
``location_terms`` 下游是精确 ``array_has_any``，这类写法永远召不回，
所以索引侧生成 ``location_terms`` 时必须归一化，且口径与查询侧词典同源。

规范形式：去掉省级前缀与「市」后缀的纯城市名（``北京`` / ``深圳``），
与 ``search/query.py`` 的地点解析结果逐字一致。
"""
from __future__ import annotations

import re

# 按省级行政区组织，便于核对覆盖面。只收地级市及以上，不含自治州/盟/地区。
_CITIES_BY_PROVINCE: dict[str, tuple[str, ...]] = {
    "直辖市": ("北京", "天津", "上海", "重庆"),
    "河北": ("石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水"),
    "山西": ("太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁"),
    "内蒙古": ("呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布"),
    "辽宁": ("沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛"),
    "吉林": ("长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城"),
    "黑龙江": ("哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化"),
    "江苏": ("南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁"),
    "浙江": ("杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水"),
    "安徽": ("合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城"),
    "福建": ("福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德"),
    "江西": ("南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶"),
    "山东": ("济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽"),
    "河南": ("郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店"),
    "湖北": ("武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州"),
    "湖南": ("长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化", "娄底"),
    "广东": ("广州", "韶关", "深圳", "珠海", "汕头", "佛山", "江门", "湛江", "茂名", "肇庆", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮"),
    "广西": ("南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池", "来宾", "崇左"),
    "海南": ("海口", "三亚", "三沙", "儋州"),
    "四川": ("成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳"),
    "贵州": ("贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁"),
    "云南": ("昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧"),
    "西藏": ("拉萨", "日喀则", "昌都", "林芝", "山南", "那曲"),
    "陕西": ("西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛"),
    "甘肃": ("兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南"),
    "青海": ("西宁", "海东"),
    "宁夏": ("银川", "石嘴山", "吴忠", "固原", "中卫"),
    "新疆": ("乌鲁木齐", "克拉玛依", "吐鲁番", "哈密"),
    "港澳": ("香港", "澳门"),
}

CITY_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(city for cities in _CITIES_BY_PROVINCE.values() for city in cities)
)
_CITY_SET = frozenset(CITY_NAMES)

# 「北上广深」这类组合写法：一次命中多个城市，查询侧直接展开成多个地点条件。
CITY_GROUPS: dict[str, tuple[str, ...]] = {
    "北上广深": ("北京", "上海", "广州", "深圳"),
    "北上广": ("北京", "上海", "广州"),
    "北上深": ("北京", "上海", "深圳"),
    "广深": ("广州", "深圳"),
}

# 省级前缀：``广东省深圳市`` → ``深圳市``。只收省/自治区/特别行政区，
# 直辖市名本身是城市名，不能当前缀剥（``上海市浦东新区``）。
PROVINCE_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        [f"{name}省" for name in _CITIES_BY_PROVINCE if name not in ("直辖市", "港澳", "内蒙古", "广西", "西藏", "宁夏", "新疆")]
        + ["内蒙古自治区", "内蒙古", "广西壮族自治区", "广西", "西藏自治区", "西藏",
           "宁夏回族自治区", "宁夏", "新疆维吾尔自治区", "新疆",
           "香港特别行政区", "澳门特别行政区"],
        key=len,
        reverse=True,
    )
)

# 首字索引：按长度倒序，取最长命中（``北京市海淀区`` → ``北京``）。
_CITY_BY_FIRST: dict[str, tuple[str, ...]] = {}
for _city in sorted(CITY_NAMES, key=len, reverse=True):
    _CITY_BY_FIRST[_city[0]] = (*_CITY_BY_FIRST.get(_city[0], ()), _city)

_SPLIT_RE = re.compile(r"[/、,，;；|·]+")
_BRACKET_RE = re.compile(r"[（(【\[〔]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 只写了区县时语义不确定（「朝阳区」多半指北京，但朝阳也是辽宁地级市），不做映射。
DISTRICT_SUFFIXES: tuple[str, ...] = ("区", "县", "镇", "乡")
_DISTRICT_SUFFIXES = frozenset(DISTRICT_SUFFIXES)


def canonical_city(value: str) -> str | None:
    """把一条地点写法规范为词典里的城市名；认不出时返回 ``None``。"""
    text = _BRACKET_RE.split(str(value or "").strip())[0].strip()
    if not text:
        return None
    if text in _CITY_SET:
        return text
    for prefix in PROVINCE_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    if text in _CITY_SET:
        return text
    for city in _CITY_BY_FIRST.get(text[0], ()):
        if not text.startswith(city):
            continue
        return None if text[len(city):][:1] in _DISTRICT_SUFFIXES else city
    return None


def normalize_location_terms(value: str | None) -> tuple[str, ...]:
    """索引侧归一化：``广东省深圳市`` → ``("深圳",)``、``成都/杭州`` → ``("成都","杭州")``。

    认不出的写法按「宁可保留」处理：中文细分（``浦东新区``）丢掉更干净，
    非中文（海外城市）原样保留；整条都认不出时保留原值，绝不把值弄空。
    只用 ``/、,;|·`` 拆分多值 —— 空格不拆，否则 ``New York`` 会被切成两个词。
    """
    raw = str(value or "").strip()
    if not raw:
        return ()
    found: list[str] = []
    for part in _SPLIT_RE.split(raw):
        part = part.strip()
        if not part:
            continue
        if part in CITY_GROUPS:
            candidates: tuple[str, ...] = CITY_GROUPS[part]
        else:
            city = canonical_city(part)
            candidates = (city,) if city else (() if _CJK_RE.search(part) else (part,))
        for candidate in candidates:
            if candidate not in found:
                found.append(candidate)
    return tuple(found) if found else (raw,)
