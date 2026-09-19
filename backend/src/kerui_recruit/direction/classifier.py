"""基于主要职责/项目证据的确定性方向分类（v4：多值职业方向 + 细分专长 + 业务方向）。

不再按固定优先级返回 high；改为对职责、项目、职位名、技能分别计分：
候选人 职责 50 / 项目 30 / 职位名 10 / 技能 10。单一证据来源（如只列技能）
不足以给出高置信方向。该分类器不调用任何模型，用于存量数据的离线回填。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

from kerui_recruit.direction.policy import (
    CAREER_DIRECTIONS,
    MAX_BUSINESS_DIRECTIONS,
    MAX_CAREER_DIRECTIONS,
    MAX_SPECIALIZATIONS_PER_DIRECTION,
    SPECIALIZATION_PARENT,
    DirectionDecision,
)

# 细分专长证据关键词（casefold 后按词边界/子串匹配）。
_SPEC_TERMS: dict[str, tuple[str, ...]] = {
    # 后端
    "BACKEND_AI_APPLICATION": ("agent", "rag", "langgraph", "langchain", "tool calling", "工具调用",
                               "大模型应用", "llm 应用", "模型 api", "prompt", "记忆模块", "planning"),
    "BACKEND_SERVICE": ("微服务", "高并发", "分布式系统", "中间件", "服务治理", "领域建模",
                        "交易系统", "支付系统", "消息队列"),
    # 前端
    "FRONTEND_WEB": ("前端", "web", "react", "vue", "组件库", "工程化", "浏览器", "css", "html"),
    "FRONTEND_CLIENT": ("ios", "android", "swift", "objective-c", "flutter", "鸿蒙", "arkts",
                        "跨端", "客户端"),
    "FRONTEND_MINI_H5": ("小程序", "h5", "公众号", "快应用"),
    # 算法
    "ALGORITHM_TRAINING": ("模型训练", "微调", "sft", "rlhf", "rlvr", "预训练", "pytorch",
                           "tensorflow", "深度学习"),
    "ALGORITHM_RECSYS": ("推荐算法", "搜索算法", "召回", "排序模型", "特征工程", "广告算法"),
    "ALGORITHM_INFERENCE": ("推理优化", "推理加速", "量化", "算子", "模型服务化", "tensorrt", "vllm"),
    "ALGORITHM_VISION_SPEECH": ("计算机视觉", "图像识别", "语音识别", "语音合成", "ocr", "目标检测"),
    # 数据
    "DATA_WAREHOUSE": ("数仓", "数据仓库", "维度建模", "主题建模", "指标体系", "olap",
                       "doris", "clickhouse"),
    "DATA_ENGINEERING": ("数据管道", "数据采集", "数据平台", "数据工程", "etl", "flink", "kafka",
                         "数据治理", "数据质量"),
    "DATA_ANALYSIS": ("数据分析", "经营分析", "报表", "bi", "tableau", "power bi", "统计分析"),
    # 运维
    "OPS_INFRA": ("kubernetes", "k8s", "容器化", "云原生", "机房", "基础设施", "terraform"),
    "OPS_SRE": ("监控告警", "故障", "稳定性", "容量规划", "变更发布", "sre", "可观测"),
    "OPS_DEVOPS": ("ci/cd", "持续集成", "持续交付", "构建发布", "效能平台", "devops", "流水线"),
    # 测试
    "QA_AUTOMATION": ("自动化测试", "测试框架", "接口测试", "单元测试", "自动化用例", "selenium", "pytest"),
    "QA_QUALITY": ("测试策略", "质量保障", "质量体系", "缺陷", "测试用例设计"),
    "QA_PERFORMANCE": ("性能测试", "压测", "性能分析", "jmeter", "locust"),
    # 产品
    "PRODUCT_PLANNING": ("需求分析", "prd", "产品规划", "路线图", "竞品", "需求评审"),
    "PRODUCT_DESIGN": ("交互设计", "体验设计", "原型", "信息架构"),
    "PRODUCT_OPERATIONS": ("增长", "产品运营", "用户运营", "活动运营", "运营策略"),
    # 管理
    "MANAGEMENT_TEAM": ("团队管理", "团队建设", "团队编制", "招聘", "绩效", "预算", "带团队"),
    "MANAGEMENT_TECH": ("技术规划", "架构决策", "技术选型", "研发管理", "技术委员会"),
}

# 全栈交付：前端与后端都必须有**实现类交付**证据；只列技术栈不算。
_FULL_STACK_FRONTEND_TERMS = ("前端", "web", "react", "vue", "小程序", "h5", "客户端",
                              "ios", "android", "flutter", "鸿蒙", "界面", "页面")
_FULL_STACK_BACKEND_TERMS = ("后端", "服务端", "api", "微服务", "java", "spring", "golang",
                             "数据库", "mysql", "redis")

# 业务方向证据关键词（casefold 后按词边界/子串匹配，只用于项目/经历的**业务场景**文本）。
#
# 严格性要求：**通用系统名词与跨域词不得单独触发**。实测教训（1721 份真实简历）：
# - 「订单」「商品」会让 26% 的简历被误判为电商，实际多是 OTA/保险/租车/风控系统；
# - 「汽车」会被服务方命中（广告平台服务汽车客户 ≠ 自己是汽车行业）；
# - 「通信」会被大型机/区块链/电力项目命中；
# - 「核心系统」「会员」「增长」「合规」「健康」「设备管理」「账务」同理，过于通用。
# 因此这里只保留领域专属词；宁缺毋滥，无证据不产出。
_BUSINESS_TERMS: dict[str, tuple[str, ...]] = {
    "BANKING": ("银行", "信贷系统", "零售银行", "银行渠道", "银行核心", "柜面"),
    "INSURANCE": ("保险", "寿险", "财险", "理赔", "保单", "承保"),
    "SECURITIES": ("证券", "基金", "资管", "投研", "行情", "交易撮合"),
    "PAYMENT": ("支付", "收单", "清结算", "通道对接", "第三方支付", "聚合支付"),
    "RISK_CREDIT": ("反欺诈", "授信", "贷后", "信用评分", "风控引擎", "风控系统"),
    "ECOMMERCE": ("电商", "商城", "跨境电商", "新零售", "店铺", "购物车"),
    "SOCIAL_CONTENT": ("社交", "社区", "内容推荐", "短视频", "直播"),
    "LOCAL_LIFE": ("到店", "外卖", "网约车", "地图", "票务", "本地生活"),
    "RETAIL": ("门店", "零售", "快消", "连锁", "商超"),
    "MARKETING": ("营销", "广告投放", "crm", "cdp", "活动运营", "广告系统"),
    "ENTERPRISE_SAAS": ("erp", "oa", "企业服务", "saas", "办公系统"),
    "DATA_INTELLIGENCE": ("bi 平台", "数据产品", "指标平台", "数据资产", "数据中台"),
    "SECURITY": ("信息安全", "网络安全", "数据安全", "渗透测试", "安全防护"),
    "MANUFACTURING": ("mes", "工业互联网", "智能制造", "生产制造", "产线", "工业软件"),
    "AUTOMOTIVE": ("车联网", "智能座舱", "三电", "自动驾驶", "整车", "主机厂",
                   "汽车电子", "汽车零部件"),
    "ENERGY": ("电网", "发电", "新能源", "储能", "电力", "碳管理"),
    "LOGISTICS": ("仓储", "wms", "tms", "运配", "供应链", "物流"),
    "GOVERNMENT": ("政务", "一网通办", "智慧城市", "公安", "政府"),
    "HEALTHCARE": ("医疗", "his", "互联网医院", "医药", "医院"),
    "EDUCATION": ("教育", "在线教育", "教务", "学习平台", "考试系统"),
    "REAL_ESTATE": ("房产", "房地产", "物业", "建筑", "bim"),
    "GAMING": ("游戏", "手游", "关卡"),
    "MEDIA": ("音视频", "内容平台", "媒体", "视频平台"),
    "TELECOM_CHIP": ("运营商", "电信", "基站", "核心网", "通信网络", "芯片", "半导体",
                     "基带", "射频"),
}

# 纯字母数字术语：按词边界匹配。子串匹配会让 app 命中 application/mapping、
# go 命中 google/django，是「全栈」被误判的主因（983 个命中里 460 个来自 app）。
_ALNUM_TERM = re.compile(r"^[a-z0-9]+$")


def _has_term(text: str, term: str) -> bool:
    """术语是否命中。

    纯字母数字术语（app / go / java / react …）按词边界匹配；含符号或空格的术语
    （``c++`` / ``.net`` / ``node.js``）保持子串匹配，避免破坏既有口径。
    """
    if not term:
        return False
    if _ALNUM_TERM.match(term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _hits(text: str, terms: tuple[str, ...]) -> int:
    folded = _fold(text)
    return sum(1 for term in terms if _has_term(folded, term.strip().casefold()))


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    folded = _fold(text)
    return any(_has_term(folded, term.strip().casefold()) for term in terms)


def _special_text(data: dict) -> str:
    parts = [
        str(data.get("summary") or ""),
        " ".join(str(s) for s in (data.get("skills") or [])),
    ]
    for exp in data.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("summary") or ""))
    for proj in data.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("summary") or ""))
            parts.append(str(proj.get("tech_stack") or ""))
            parts.append(str(proj.get("name") or ""))
    return " ".join(parts).casefold()


def _delivery_text(data: dict) -> str:
    """实现类交付证据：职责/项目描述，**刻意不含技术栈列表**。

    全栈判定要求「同时交付前后端」，技术栈里同时写了 React 与 Java 只是技能面证据，
    不足以证明两侧都有交付，因此这里排除 tech_stack。
    """
    parts = [str(data.get("summary") or "")]
    for exp in data.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("summary") or ""))
    for proj in data.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("summary") or ""))
            parts.append(str(proj.get("business_scene") or ""))
            parts.append(str(proj.get("name") or ""))
    return " ".join(parts).casefold()


def _project_business_text(data: dict) -> str:
    """项目业务场景文本（业务方向的**主要**证据来源）。"""
    parts: list[str] = []
    for proj in data.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("business_scene") or ""))
            parts.append(str(proj.get("summary") or ""))
            parts.append(str(proj.get("name") or ""))
    return " ".join(parts).casefold()


def _business_scores(text: str) -> list[tuple[int, str]]:
    """按关键词命中数给业务方向打分，返回按证据强度排序的 (score, code)。"""
    ranked = [(_hits(text, terms), code) for code, terms in _BUSINESS_TERMS.items()]
    return sorted((item for item in ranked if item[0] > 0), key=lambda item: (-item[0], item[1]))


_DATE_RE = re.compile(r"(\d{4})\s*[.\-/年]?\s*(\d{1,2})?")


def _parse_month(value: str) -> int | None:
    """把 ``2020.07`` / ``2020-7`` / ``2020年7月`` / ``2020`` 解析为年*12+月的序数。"""
    match = _DATE_RE.search(value or "")
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2) or 1) - 1


def _is_present(end_text: str) -> bool:
    folded = (end_text or "").casefold()
    return not folded or any(marker in folded for marker in ("至今", "present", "now", "当前"))


def _experience_duration(exp: dict) -> int:
    """任职时长（月）；无法解析起止时间返回 0。"""
    start = _parse_month(str(exp.get("start_date") or ""))
    if start is None:
        return 0
    end_text = str(exp.get("end_date") or "")
    today = datetime.now()
    end = today.year * 12 + today.month - 1 if _is_present(end_text) else _parse_month(end_text)
    if end is None or end < start:
        return 0
    return end - start


def _slot_experiences(experiences: list[dict]) -> list[dict]:
    """返回「最近一段」与「任职时长最长的一段」经历（去重，可能只有 1 条或 0 条）。"""
    if not experiences:
        return []
    present = [exp for exp in experiences if _is_present(str(exp.get("end_date") or ""))]
    if present:
        recent = present[0]
    else:
        recent = max(
            experiences,
            key=lambda exp: _parse_month(str(exp.get("end_date") or exp.get("start_date") or "")) or 0,
        )
    longest = max(experiences, key=_experience_duration)
    return [recent] if longest is recent else [recent, longest]


def classify_specializations(
    data: dict,
    directions: tuple[str, ...] | list[str] | str | None,
) -> tuple[str, ...]:
    """基于职责/项目证据判定**职业方向细分**（新词表，多值）。

    规则（与合同一致）：
    - 细分必须归属已选职业大类，否则丢弃；
    - 每个大类下最多 ``MAX_SPECIALIZATIONS_PER_DIRECTION`` 个，按关键词命中数排序取前 N；
    - ``BACKEND_FULL_STACK`` 需职责/项目里**同时**出现前端与后端的实现类交付证据，
      只列技术栈不算；其证据强度取两侧命中数之和。
    """
    allowed = (directions,) if isinstance(directions, str) else tuple(directions or ())
    if not allowed:
        return ()
    allowed_set = set(allowed)
    text = _special_text(data)
    per_direction: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for code, terms in _SPEC_TERMS.items():
        parent = SPECIALIZATION_PARENT[code]
        if parent not in allowed_set:
            continue
        score = _hits(text, terms)
        if score:
            per_direction[parent].append((score, code))

    if "BACKEND" in allowed_set:
        delivery = _delivery_text(data)
        frontend_hits = _hits(delivery, _FULL_STACK_FRONTEND_TERMS)
        backend_hits = _hits(delivery, _FULL_STACK_BACKEND_TERMS)
        if frontend_hits and backend_hits:
            per_direction["BACKEND"].append((frontend_hits + backend_hits, "BACKEND_FULL_STACK"))

    result: list[str] = []
    for parent in allowed:
        ranked = sorted(per_direction.get(parent, []), key=lambda item: (-item[0], item[1]))
        for _, code in ranked[:MAX_SPECIALIZATIONS_PER_DIRECTION]:
            if code not in result:
                result.append(code)
    return tuple(result)


def classify_business_directions(data: dict) -> tuple[str, ...]:
    """依据项目/经历的业务场景判定业务方向（最多 ``MAX_BUSINESS_DIRECTIONS`` 个）。

    口径（与合同一致）：先取「最近一段经历」与「任职时长最长的一段经历」各自业务证据最强的
    1 个并去重；不足 2 个时，用全部项目的业务场景按证据强度补齐。

    ``ParsedProject`` 没有起止日期，因此项目的「最近/最长」无法直接判定：这里用
    **所属工作经历**作为时段代理，并让项目证据参与补齐，保证项目仍是主要证据来源。
    """
    experiences = [exp for exp in (data.get("experiences") or []) if isinstance(exp, dict)]
    picked: list[str] = []

    for exp in _slot_experiences(experiences):
        text = _fold(" ".join((str(exp.get("summary") or ""), str(exp.get("industry") or ""))))
        ranked = _business_scores(text)
        if ranked and ranked[0][1] not in picked:
            picked.append(ranked[0][1])

    if len(picked) < MAX_BUSINESS_DIRECTIONS:
        for _, code in _business_scores(_project_business_text(data)):
            if code not in picked:
                picked.append(code)
            if len(picked) >= MAX_BUSINESS_DIRECTIONS:
                break

    return tuple(picked[:MAX_BUSINESS_DIRECTIONS])

# 方向决定性/支持性关键词（casefold 后子串匹配）。技术词只作弱信号，职责/项目是主证据。
_TERMS: dict[str, tuple[str, ...]] = {
    "BACKEND": (
        "服务端", "后端", "api", "微服务", "高并发", "数据平台", "数据管道",
        "数据采集", "数据工程", "交易系统", "支付", "服务工程", "agent 编排",
        "agent 应用", "工具调用", "tool calling", "工作流编排", "rag",
        "langchain", "langgraph", "java", "spring", "go ", "golang", "dubbo",
        "kafka", "flink", "mysql", "redis", "消息队列", "分布式系统", "中间件",
        "spring boot", "spring cloud",
    ),
    "FRONTEND": (
        "前端", "客户端", "web ", "react", "vue", "ios", "android", "swift",
        "objective-c", "arkts", "小程序", "h5", "界面", "组件", "跨端", "sdk",
        "typescript", "javascript", "html", "css", "flutter", "鸿蒙", "app",
    ),
    "ALGORITHM": (
        "模型训练", "算法研发", "微调", "sft", "rlvr", "dapo", "推荐算法",
        "搜索算法", "推理优化", "深度学习", "机器学习", "预训练", "pytorch",
        "tensorflow", "特征工程", "离线评测", "llm 训练", "大模型训练",
    ),
    "DATA": (
        "数仓", "数据仓库", "bi", "数据分析", "指标体系", "统计分析", "报表",
        "经营分析", "olap", "主题建模", "维度建模", "tableau", "power bi",
        "数据治理", "etl", "sql", "hive", "clickhouse", "doris",
    ),
    "OPS": (
        "运维", "sre", "监控告警", "发布系统", "基础设施", "kubernetes",
        "docker", "容器化", "云原生", "稳定性", "故障", "devops", "terraform",
    ),
    "QA": ("测试开发", "自动化测试", "质量保障", "qa", "缺陷", "测试策略"),
    "PRODUCT": ("产品经理", "产品设计", "产品规划", "prd", "路线图", "需求分析", "产品需求"),
    # 管理仅作强管理交付的弱信号；是否计入 management 属性另见 _STRONG_MANAGEMENT。
    "MANAGEMENT": ("团队管理", "团队建设", "研发管理", "技术管理", "招聘", "绩效", "预算"),
}

# 这些词一旦在职责/职位名出现，即标记 management=true（与技术专业并存）。
_STRONG_MANAGEMENT = (
    "团队管理", "团队建设", "团队编制", "招聘", "培养", "绩效", "预算", "跨团队交付",
    "管理团队", "带领团队", "下属",
)

# 候选人的证据源权重（总分 100）。
_DUTY_WEIGHT = 50
_PROJECT_WEIGHT = 30
_TITLE_WEIGHT = 10
_SKILL_WEIGHT = 10


def _fold(text: str) -> str:
    return text.casefold()


def _distribute(text: str, weight: float) -> dict[str, float]:
    """按各方向关键词命中数，把一个证据源的权重按比例分给各方向。"""
    counts = {direction: _hits(text, terms) for direction, terms in _TERMS.items()}
    total = sum(counts.values())
    if total == 0:
        return {}
    return {direction: weight * (count / total) for direction, count in counts.items() if count > 0}


def _as_str(value: object) -> str:
    return str(value or "")


def _scoring_view(data: dict) -> dict:
    """把简历 / JD 两种 parsed_data 形态统一成打分器需要的字段。

    JD 解析结果的字段名与简历不同（``core_duties`` / ``candidate_profile`` /
    ``required_skills``），若直接喂给打分器会**全部判空**——岗位侧的方向与硬门槛
    会因此整体失效。这里做一次显式映射，映射后 JD 与简历走同一套打分逻辑：

    - 每条核心职责视为一段"经历"（职责是主证据，权重 50）；
    - 岗位候选人画像视为一条"项目"（画像级证据，权重 30）；
    - 必备技能 + 加分技能作为技能证据；岗位名称作为职位名证据。
    """
    is_jd = ("experiences" not in data) and ("core_duties" in data or "required_skills" in data)
    if not is_jd:
        return data

    duties = [str(d).strip() for d in (data.get("core_duties") or []) if str(d).strip()]
    skills = [str(s).strip() for s in (data.get("required_skills") or []) if str(s).strip()]
    skills += [str(s).strip() for s in (data.get("plus_skills") or []) if str(s).strip()]
    industry = str(data.get("industry") or "").strip()
    profile = str(
        data.get("candidate_profile_narrative") or data.get("candidate_profile") or ""
    ).strip()

    return {
        "current_title": data.get("title"),
        "summary": profile or str(data.get("summary") or ""),
        "experiences": [{"summary": d, "industry": industry or None} for d in duties],
        "projects": [{"summary": profile}] if profile else [],
        "skills": skills,
        "industry": industry or None,
    }


def classify_direction(data: dict) -> DirectionDecision:
    """基于结构化证据的确定性方向分类（简历与 JD 共用）。

    返回 ``DirectionDecision``：``direction`` 为主方向、``secondary`` 为次方向，
    ``career_directions`` 为多值职业大类（最多 ``MAX_CAREER_DIRECTIONS`` 个）、
    ``specializations`` 为细分专长（新词表）、``business_directions`` 为业务方向，
    ``management`` 记录管理属性。证据不足时主方向为 None、置信 low。
    """
    data = _scoring_view(data)
    scores: dict[str, float] = defaultdict(float)
    evidence: dict[str, list[str]] = defaultdict(list)

    # 1) 职责：近期经历 summary（主证据，权重 50）
    experiences = [e for e in (data.get("experiences") or []) if isinstance(e, dict)]
    if experiences:
        per = _DUTY_WEIGHT / len(experiences)
        for i, exp in enumerate(experiences):
            text = _as_str(exp.get("summary"))
            for direction, score in _distribute(text, per).items():
                scores[direction] += score
                evidence[direction].append(f"experiences[{i}].summary")

    # 2) 项目产出（权重 30）
    projects = [p for p in (data.get("projects") or []) if isinstance(p, dict)]
    if projects:
        per = _PROJECT_WEIGHT / len(projects)
        for i, proj in enumerate(projects):
            text = _as_str(proj.get("summary")) + " " + _as_str(proj.get("tech_stack")) + " " + _as_str(proj.get("name"))
            for direction, score in _distribute(text, per).items():
                scores[direction] += score
                evidence[direction].append(f"projects[{i}].summary")

    # 3) 职位名（权重 10）
    title = _as_str(data.get("current_title")) or (experiences[0].get("title") if experiences else "")
    if title:
        for direction, score in _distribute(_as_str(title), _TITLE_WEIGHT).items():
            scores[direction] += score
            evidence[direction].append("current_title" if data.get("current_title") else "experiences[0].title")

    # 4) 技能（权重 10，弱证据）
    skills = " ".join(_as_str(s) for s in (data.get("skills") or []))
    if skills:
        for direction, score in _distribute(skills, _SKILL_WEIGHT).items():
            scores[direction] += score
            evidence[direction].append("skills")

    # 管理属性：强管理词出现即标记（与技术专业并存）。
    duty_title_text = " ".join([_as_str(title), *[_as_str(e.get("summary")) for e in experiences]])
    management = any(term in _fold(duty_title_text) for term in _STRONG_MANAGEMENT)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] <= 0:
        return DirectionDecision(None, "low", (), secondary=None, management=management,
                                 business_directions=classify_business_directions(data))

    primary, primary_score = ranked[0]
    secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0 else None
    secondary_score = ranked[1][1] if secondary else 0.0

    # 置信度：>=70 且领先 >=20 且来自 >=2 类独立证据源 → high；50-69 → medium；<50 → low。
    source_kinds = {path.split("[")[0] for path in evidence[primary]}
    if primary_score >= 70 and (primary_score - secondary_score) >= 20 and len(source_kinds) >= 2:
        confidence = "high"
    elif primary_score >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    # 细分按**全量词表**判定：若先用打分出来的大类列表去限制，某大类一旦被挤出前 2，
    # 它的细分会在源头被整体丢弃（实测 287 份简历因此没有细分）。
    specializations = classify_specializations(data, CAREER_DIRECTIONS)

    spec_parents: list[str] = []
    for code in specializations:
        parent = SPECIALIZATION_PARENT[code]
        if parent not in spec_parents:
            spec_parents.append(parent)

    # 大类顺序：**打分主方向优先**（保证单值 direction 镜像稳定、不带迁移噪音），
    # 剩余名额优先给有细分支撑的大类，避免细分因「父大类未入选」被丢弃。
    ordered: list[str] = []
    for direction in (*(name for name, _ in ranked), *spec_parents):
        if direction != "OTHER" and direction not in ordered:
            ordered.append(direction)
    career_directions = tuple(ordered[:MAX_CAREER_DIRECTIONS]) or ("OTHER",)

    specializations = tuple(dict.fromkeys(
        code for code in specializations if SPECIALIZATION_PARENT[code] in career_directions))

    return DirectionDecision(
        primary,
        confidence,
        tuple(dict.fromkeys(evidence[primary])),
        secondary=secondary,
        management=management,
        specializations=specializations,
        career_directions=career_directions,
        business_directions=classify_business_directions(data),
    )
