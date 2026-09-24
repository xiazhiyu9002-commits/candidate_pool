"""职业方向统一分类：合法枚举、归一与判定契约。

三层词表：
- ``VALID_DIRECTIONS``：职业方向**大类**（9 个，含兜底 OTHER），历史单值语义保留；
- ``CAREER_SPECIALIZATIONS``：职业方向**细分**（24 个），每个细分归属唯一的职业大类，
  代码以大类名为前缀，便于由细分反推大类；
- ``BUSINESS_DIRECTIONS``：**业务方向**（25 个），依据项目/经历的业务场景判定，
  与 ``search.industry`` 的行业桶相互独立（后者仍用于匹配打分与画像输入）。
"""
from __future__ import annotations

from dataclasses import dataclass

# 合法职业方向大类（简历与 JD 共用）。非法值一律归待核，不直接入索引。
VALID_DIRECTIONS = (
    "BACKEND",
    "FRONTEND",
    "ALGORITHM",
    "DATA",
    "OPS",
    "QA",
    "PRODUCT",
    "MANAGEMENT",
    "OTHER",
)

# 职业方向大类（新命名，与 VALID_DIRECTIONS 同集合；语义上用于多值场景）。
CAREER_DIRECTIONS = VALID_DIRECTIONS

# 旧版细分职业专长标签（多值）。已被 CAREER_SPECIALIZATIONS 取代，保留供存量数据兼容。
SPECIALIZATIONS = (
    "FULL_STACK",
    "AI_APPLICATION",
    "DATA_PLATFORM",
    "DATA_WAREHOUSE",
)

# 旧细分 -> 新细分映射（存量数据迁移与离线回填使用）。
LEGACY_SPECIALIZATION_MAP: dict[str, str] = {
    "FULL_STACK": "BACKEND_FULL_STACK",
    "AI_APPLICATION": "BACKEND_AI_APPLICATION",
    "DATA_PLATFORM": "DATA_ENGINEERING",
    "DATA_WAREHOUSE": "DATA_WAREHOUSE",
}

# 职业方向细分 -> 所属职业大类。细分代码以大类为前缀，全局唯一。
SPECIALIZATION_PARENT: dict[str, str] = {
    # 后端
    "BACKEND_SERVICE": "BACKEND",
    "BACKEND_AI_APPLICATION": "BACKEND",
    "BACKEND_FULL_STACK": "BACKEND",
    # 前端
    "FRONTEND_WEB": "FRONTEND",
    "FRONTEND_CLIENT": "FRONTEND",
    "FRONTEND_MINI_H5": "FRONTEND",
    # 算法
    "ALGORITHM_TRAINING": "ALGORITHM",
    "ALGORITHM_RECSYS": "ALGORITHM",
    "ALGORITHM_INFERENCE": "ALGORITHM",
    "ALGORITHM_VISION_SPEECH": "ALGORITHM",
    # 数据
    "DATA_WAREHOUSE": "DATA",
    "DATA_ENGINEERING": "DATA",
    "DATA_ANALYSIS": "DATA",
    # 运维
    "OPS_INFRA": "OPS",
    "OPS_SRE": "OPS",
    "OPS_DEVOPS": "OPS",
    # 测试
    "QA_AUTOMATION": "QA",
    "QA_QUALITY": "QA",
    "QA_PERFORMANCE": "QA",
    # 产品
    "PRODUCT_PLANNING": "PRODUCT",
    "PRODUCT_DESIGN": "PRODUCT",
    "PRODUCT_OPERATIONS": "PRODUCT",
    # 管理
    "MANAGEMENT_TEAM": "MANAGEMENT",
    "MANAGEMENT_TECH": "MANAGEMENT",
}

CAREER_SPECIALIZATIONS = tuple(SPECIALIZATION_PARENT)

# 中文标签（提示词与前端展示共用同一份口径）。
CAREER_DIRECTION_LABELS: dict[str, str] = {
    "BACKEND": "后端",
    "FRONTEND": "前端",
    "ALGORITHM": "算法",
    "DATA": "数据",
    "OPS": "运维",
    "QA": "测试",
    "PRODUCT": "产品",
    "MANAGEMENT": "管理",
    "OTHER": "其他",
}

SPECIALIZATION_LABELS: dict[str, str] = {
    "BACKEND_SERVICE": "服务端架构",
    "BACKEND_AI_APPLICATION": "AI 应用集成",
    "BACKEND_FULL_STACK": "全栈交付",
    "FRONTEND_WEB": "Web 前端",
    "FRONTEND_CLIENT": "客户端",
    "FRONTEND_MINI_H5": "小程序与 H5",
    "ALGORITHM_TRAINING": "模型训练与微调",
    "ALGORITHM_RECSYS": "推荐与搜索算法",
    "ALGORITHM_INFERENCE": "推理与部署优化",
    "ALGORITHM_VISION_SPEECH": "视觉与语音",
    "DATA_WAREHOUSE": "数仓与建模",
    "DATA_ENGINEERING": "数据开发与管道",
    "DATA_ANALYSIS": "业务分析",
    "OPS_INFRA": "基础设施与云原生",
    "OPS_SRE": "SRE 与稳定性",
    "OPS_DEVOPS": "研发效能与工具链",
    "QA_AUTOMATION": "自动化测试",
    "QA_QUALITY": "质量保障",
    "QA_PERFORMANCE": "性能测试",
    "PRODUCT_PLANNING": "产品规划",
    "PRODUCT_DESIGN": "产品设计",
    "PRODUCT_OPERATIONS": "产品运营",
    "MANAGEMENT_TEAM": "团队管理",
    "MANAGEMENT_TECH": "技术管理",
}

BUSINESS_DIRECTION_LABELS: dict[str, str] = {
    "BANKING": "银行",
    "INSURANCE": "保险",
    "SECURITIES": "证券与投资",
    "PAYMENT": "支付与清结算",
    "RISK_CREDIT": "风控与信贷",
    "ECOMMERCE": "电商",
    "SOCIAL_CONTENT": "社交与内容",
    "LOCAL_LIFE": "本地生活与出行",
    "RETAIL": "零售与快消",
    "MARKETING": "营销与广告",
    "ENTERPRISE_SAAS": "企业软件与 SaaS",
    "DATA_INTELLIGENCE": "数据智能与 BI",
    "SECURITY": "安全与合规",
    "MANUFACTURING": "制造与工业",
    "AUTOMOTIVE": "汽车",
    "ENERGY": "能源与电力",
    "LOGISTICS": "物流与供应链",
    "GOVERNMENT": "政务与智慧城市",
    "HEALTHCARE": "医疗健康",
    "EDUCATION": "教育",
    "REAL_ESTATE": "房地产与建筑",
    "GAMING": "游戏",
    "MEDIA": "文娱传媒",
    "TELECOM_CHIP": "通信与芯片",
    "OTHER": "其他",
}

# 合法业务方向。依据项目/经历的业务场景判定，与行业桶（search.industry）相互独立。
BUSINESS_DIRECTIONS = (
    # 金融与交易
    "BANKING",
    "INSURANCE",
    "SECURITIES",
    "PAYMENT",
    "RISK_CREDIT",
    # 互联网与消费
    "ECOMMERCE",
    "SOCIAL_CONTENT",
    "LOCAL_LIFE",
    "RETAIL",
    "MARKETING",
    # 企业服务与安全
    "ENTERPRISE_SAAS",
    "DATA_INTELLIGENCE",
    "SECURITY",
    # 实体产业
    "MANUFACTURING",
    "AUTOMOTIVE",
    "ENERGY",
    "LOGISTICS",
    # 公共服务
    "GOVERNMENT",
    "HEALTHCARE",
    "EDUCATION",
    "REAL_ESTATE",
    # 其他
    "GAMING",
    "MEDIA",
    "TELECOM_CHIP",
    "OTHER",
)

# 数量上限：候选人职业大类 ≤2，岗位 JD 职业大类 ≤3；
# 每个大类下细分 ≤2（候选人细分总数因此 ≤4），业务方向 ≤2。
MAX_CAREER_DIRECTIONS = 2
MAX_CAREER_DIRECTIONS_JD = 3
MAX_SPECIALIZATIONS_PER_DIRECTION = 2
MAX_BUSINESS_DIRECTIONS = 2

TAXONOMY_VERSION = "4"


def is_valid_direction(value: str | None) -> bool:
    """是否合法方向；None（无证据）合法。"""
    return value is None or value in VALID_DIRECTIONS


def normalize_direction(value: str | None) -> str | None:
    """把任意值归一为合法方向；非法值（如 OPERATIONS）归 None（待核）。"""
    if value in VALID_DIRECTIONS:
        return value
    return None


def is_pending_direction(value: object) -> bool:
    """方向是否待核：缺失、OTHER 或非法值均需人工确认/异步重判。"""
    return value not in VALID_DIRECTIONS or value == "OTHER"


def is_pending_career(values: object) -> bool:
    """多值职业方向是否待核：空、或全部为 OTHER/非法值。

    容忍单值字符串输入（过渡期旧数据仍是单值 `direction`）。
    """
    if values is None or values == "":
        return True
    candidates = [values] if isinstance(values, str) else values
    if not isinstance(candidates, (list, tuple, set)):
        return True
    return not any(v in VALID_DIRECTIONS and v != "OTHER" for v in candidates)


def is_valid_specialization(value: str | None) -> bool:
    return value in SPECIALIZATION_PARENT


def is_valid_business_direction(value: str | None) -> bool:
    return value in BUSINESS_DIRECTIONS


def specialization_parent(value: str) -> str | None:
    """细分所属职业大类；非法细分返回 None。"""
    return SPECIALIZATION_PARENT.get(value)


def _ordered_uniq(values: object) -> list[str]:
    """把任意输入压成去重保序的字符串列表（兼容 None / str / list）。"""
    if values is None:
        return []
    if isinstance(values, str):
        candidates: list[object] = [values]
    elif isinstance(values, (list, tuple, set)):
        candidates = list(values)
    else:
        candidates = [values]
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        text = str(item).strip() if item is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_career(
    directions: object,
    specializations: object = (),
    *,
    max_directions: int = MAX_CAREER_DIRECTIONS,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把 LLM 输出的职业方向合法化并限流。

    规则（与合同一致）：
    - 大类取合法枚举、去重保序；超过 ``max_directions`` 时，优先保留有细分支撑的大类；
    - 细分取合法枚举、去重保序；其所属大类必须已被保留，否则丢弃；
    - 同一大类下细分超过 ``MAX_SPECIALIZATIONS_PER_DIRECTION`` 时按输入顺序截断；
    - 旧版细分代码（FULL_STACK 等）自动映射为新代码。

    ``max_directions`` 默认取候选人上限；岗位 JD 传 ``MAX_CAREER_DIRECTIONS_JD``。
    """
    ordered = [d for d in _ordered_uniq(directions) if d in VALID_DIRECTIONS]

    specs = [
        LEGACY_SPECIALIZATION_MAP.get(s, s)
        for s in _ordered_uniq(specializations)
    ]
    specs = list(dict.fromkeys(s for s in specs if s in SPECIALIZATION_PARENT))

    if len(ordered) > max_directions:
        with_specs = [d for d in ordered if any(SPECIALIZATION_PARENT[s] == d for s in specs)]
        rest = [d for d in ordered if d not in with_specs]
        ordered = (with_specs + rest)[:max_directions]

    kept_directions = set(ordered)
    per_direction: dict[str, int] = {}
    kept_specs: list[str] = []
    for spec in specs:
        parent = SPECIALIZATION_PARENT[spec]
        if parent not in kept_directions:
            continue
        if per_direction.get(parent, 0) >= MAX_SPECIALIZATIONS_PER_DIRECTION:
            continue
        per_direction[parent] = per_direction.get(parent, 0) + 1
        kept_specs.append(spec)

    return tuple(ordered), tuple(kept_specs)


def normalize_business_directions(values: object) -> tuple[str, ...]:
    """业务方向合法化 + 去重保序 + 限流到 ``MAX_BUSINESS_DIRECTIONS``。"""
    valid = [v for v in _ordered_uniq(values) if v in BUSINESS_DIRECTIONS]
    return tuple(valid[:MAX_BUSINESS_DIRECTIONS])


def extract_multi_directions(
    data: dict,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """从 parsed_data 读取多值方向（职业大类、职业细分、业务方向）。

    优先取顶层字段；存量数据只有旧结构时回退到 ``direction_assessment``，
    再回退到单值 ``direction``。只做去重保序，不做枚举过滤与限流——读侧不丢存量标签，
    词表迁移与限流由解析/回填侧负责。
    """
    assessment = data.get("direction_assessment") or {}
    career = data.get("career_directions") or assessment.get("career_directions") or ()
    specs = data.get("career_specializations") or assessment.get("specializations") or ()
    business = data.get("business_directions") or assessment.get("business_directions") or ()
    if not career and data.get("direction"):
        career = (data["direction"],)
    return (
        tuple(_ordered_uniq(career)),
        tuple(_ordered_uniq(specs)),
        tuple(_ordered_uniq(business)),
    )


def confirmed_multi_directions(
    data: dict,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """可参与**硬条件与打分**的多值方向（职业大类、职业细分、业务方向）。

    与 ``extract_multi_directions`` 的区别是这里做可用性收敛：
    - 剔除 ``OTHER`` 与非法大类——OTHER 是兜底值，拿它参与交集会误拒（语义上等于"未知"）；
    - 细分只保留新词表代码：旧版专长代码（``FULL_STACK`` 等）与新词表不可比，
      若强行比较会出现「两边各有细分但词表不同 → 误判不一致」，因此交由调用方回退大类比较；
    - 业务方向同样剔除 ``OTHER`` 与非法值。
    """
    career, specs, business = extract_multi_directions(data)
    return (
        tuple(v for v in career if v in VALID_DIRECTIONS and v != "OTHER"),
        tuple(v for v in specs if v in SPECIALIZATION_PARENT),
        tuple(v for v in business if v in BUSINESS_DIRECTIONS and v != "OTHER"),
    )


def sync_assessment(
    assessment: dict | None,
    career: tuple[str, ...],
    specializations: tuple[str, ...],
    business: tuple[str, ...],
) -> dict | None:
    """把多值方向同步进 ``direction_assessment``（缺省不新建，避免凭空造结构）。"""
    if not isinstance(assessment, dict):
        return assessment
    assessment["specializations"] = list(specializations)
    assessment["career_directions"] = list(career)
    assessment["business_directions"] = list(business)
    return assessment


def apply_direction_normalization(
    parsed: dict,
    *,
    max_directions: int = MAX_CAREER_DIRECTIONS,
) -> None:
    """多值方向合法化 + 限流 + 镜像同步（就地修改 ``parsed``）。

    编辑接口直接把字段写进 ``parsed_data``，不会保留 pydantic 校验器的归一结果，
    因此需要显式补一次；否则会出现「改了 ``career_directions`` 但单值 ``direction``
    仍是旧值」或「超限的大类/细分被写进索引」这类不一致。

    ``max_directions`` 默认取候选人上限；JD 侧传 ``MAX_CAREER_DIRECTIONS_JD``。
    """
    career, specializations = normalize_career(
        parsed.get("career_directions"), parsed.get("career_specializations"),
        max_directions=max_directions,
    )
    business = normalize_business_directions(parsed.get("business_directions"))
    parsed["career_directions"] = list(career)
    parsed["career_specializations"] = list(specializations)
    parsed["business_directions"] = list(business)
    parsed["career_taxonomy_version"] = TAXONOMY_VERSION
    primary = next((value for value in career if value != "OTHER"), None)
    if primary is not None:
        parsed["direction"] = primary
    parsed["direction_assessment"] = sync_assessment(
        parsed.get("direction_assessment"), career, specializations, business)


def render_career_taxonomy() -> str:
    """渲染「大类 -> 可选细分」词表，供提示词使用（单一数据源，避免词表漂移）。"""
    lines: list[str] = []
    for direction in VALID_DIRECTIONS:
        specs = [s for s, parent in SPECIALIZATION_PARENT.items() if parent == direction]
        if not specs:
            lines.append(f"- {direction}（{CAREER_DIRECTION_LABELS[direction]}）：无细分")
            continue
        rendered = "、".join(f"{code}（{SPECIALIZATION_LABELS[code]}）" for code in specs)
        lines.append(f"- {direction}（{CAREER_DIRECTION_LABELS[direction]}）：{rendered}")
    return "\n".join(lines)


def direction_field_for_code(code: str) -> str | None:
    """枚举 code 属于哪个筛选字段：职业大类 / 职业细分 / 业务方向；未命中返回 None。

    查询解析用：LLM 输出的方向 code 必须落回正确的硬筛选字段，否则丢掉。
    """
    text = str(code or "").strip()
    if not text:
        return None
    if text in CAREER_DIRECTION_LABELS:
        return "career_directions"
    if text in SPECIALIZATION_LABELS:
        return "career_specializations"
    if text in BUSINESS_DIRECTION_LABELS:
        return "business_directions"
    return None


_DIRECTION_LABEL_TO_CODE: dict[str, str] = {
    **{label.casefold(): code for code, label in CAREER_DIRECTION_LABELS.items()},
    **{label.casefold(): code for code, label in SPECIALIZATION_LABELS.items()},
    **{label.casefold(): code for code, label in BUSINESS_DIRECTION_LABELS.items()},
}


def normalize_direction_value(value: str) -> str | None:
    """方向字段的中文标签 / code → 合法 code；未知值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if direction_field_for_code(text) is not None:
        return text
    return _DIRECTION_LABEL_TO_CODE.get(text.casefold())


def render_business_taxonomy() -> str:
    """渲染业务方向词表，供提示词使用（单一数据源）。"""
    return "、".join(f"{code}（{BUSINESS_DIRECTION_LABELS[code]}）" for code in BUSINESS_DIRECTIONS)


@dataclass(frozen=True, slots=True)
class DirectionDecision:
    """一次方向判定：主方向 + 次方向 + 置信 + 证据路径 + 管理属性 + 分类版本。

    ``direction`` 保留旧字段语义（等于主方向），便于老数据与前端兼容；
    ``assessment()`` 输出 v2 的 ``direction_assessment`` 结构。
    """

    direction: str | None
    confidence: str  # "high" | "medium" | "low"
    evidence: tuple[str, ...] = ()
    taxonomy_version: str = TAXONOMY_VERSION
    secondary: str | None = None
    management: bool = False
    specializations: tuple[str, ...] = ()
    # 多值职业大类与业务方向（与 direction/specializations 同源，direction 为首个大类）。
    career_directions: tuple[str, ...] = ()
    business_directions: tuple[str, ...] = ()

    def assessment(self) -> dict:
        return {
            "primary": self.direction,
            "secondary": self.secondary,
            "confidence": self.confidence,
            "evidence_paths": list(self.evidence),
            "management": self.management,
            "specializations": list(self.specializations),
            "taxonomy_version": self.taxonomy_version,
            "career_directions": list(self.career_directions or ((self.direction,) if self.direction else ())),
            "business_directions": list(self.business_directions),
        }
