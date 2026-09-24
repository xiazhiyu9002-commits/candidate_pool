"""JD 候选人画像中的显式硬条件解析与评估。

把画像文本里的明确硬条件（卡 985、字节或阿里背景、LangGraph 优先）解析成可检查、
可修改的结构化约束。约束只来自人工画像文本或明确语义，不把「优先」升级为 MUST。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from kerui_recruit.search.degrees import degrees_at_least, normalize_degree

# 学校档次别名：画像里常见的表达归一为结构化可核验值。
# 只放**学校档次**；「硕士/博士」属于学历门槛，见 _DEGREE_ALIASES —— 混进这一维度会导致
# 「必须硕士及以上」被当成学校档次约束（拿 "硕士" 去比学校标签，任何人都不命中 → 全库误拒）。
_SCHOOL_LEVEL_ALIASES: dict[str, tuple[str, ...]] = {
    "985": ("985", "985高校"),
    "211": ("211", "211高校"),
    "双一流": ("双一流",),
}

# 学历门槛别名：归一为 search/degrees.py 的规范值（COLLEGE / BACHELOR / MASTER / DOCTOR）。
# 英文写法只在**完整学位表述**时收录：「bachelor」「master degree」「phd」——
# 不收裸 "master"，否则「Master Data（主数据）」「master 分支」会被误判成硕士。
_DEGREE_ALIASES: dict[str, tuple[str, ...]] = {
    "大专": ("大专", "专科", "associate degree"),
    "本科": ("本科", "bachelor", "bachelor's degree", "bachelor degree"),
    "硕士": ("硕士", "研究生", "master's degree", "masters degree", "master degree"),
    "博士": ("博士", "phd", "ph.d", "doctor degree", "doctoral degree"),
}

# 公司经历别名：**判定用**的同义名扩展。约束里的 alternatives 由模型按原文点名公司产出
# （「只要字节、阿里背景」→ alternatives 为 字节/阿里），这里负责把中英文写法对上候选人的
# 公司文本（如 字节 ↔ 字节跳动 / ByteDance、甲骨文 ↔ Oracle）。
_COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "字节": ("字节跳动", "bytedance"),
    "阿里": ("阿里巴巴", "alibaba"),
    "腾讯": ("tencent",),
    "蚂蚁": ("蚂蚁集团", "antgroup"),
    "百度": ("baidu",),
    "美团": ("美团点评", "meituan"),
    "京东": ("京东集团", "jd.com"),
    "华为": ("huawei",),
    "网易": ("netease",),
    "小米": ("xiaomi",),
    "快手": ("kuaishou",),
    "滴滴": ("滴滴出行", "didi"),
    "拼多多": ("pdd",),
    "微软": ("microsoft",),
    "谷歌": ("google",),
    "亚马逊": ("amazon", "aws"),
    "甲骨文": ("oracle",),
    "苹果": ("apple",),
}

# 抽取阶段就认识的**无歧义英文公司名**：这些写法在 JD 里只会指公司，不会是技术/产品。
# 反例（刻意排除）：oracle（数据库）、aws / amazon（云服务）、google（Google Cloud / Vertex）、
# microsoft（Microsoft Azure）、apple（Apple Pay）——把它们当公司抽会产出错误的硬条件。
_UNIQUE_ENGLISH_COMPANIES = frozenset({
    "bytedance", "alibaba", "tencent", "antgroup", "meituan", "kuaishou",
    "pinduoduo", "xiaomi", "netease", "didi", "huawei", "baidu", "jd.com",
})

# 明确硬条件触发词（出现在画像中即视为 MUST），避免把普通描述误判为硬条件。
# 「及以上 / 以上」是门槛表达（「本科及以上」「985 本科及以上」），同样算明确要求。
_MUST_MARKERS = ("卡", "必须", "硬性要求", "必要条件", "缺一不可", "必备", "及以上", "以上")
# 优先/加分触发词 → PLUS。
_PLUS_MARKERS = ("优先", "加分", "更佳", "最好", "prefer", "plus")
# 排除/否定触发词 → EXCLUDE。缺了这层，「不考虑阿里背景」会按默认 MUST 处理，
# 把排除项**反转成必须项**——JD 说不看阿里的人，系统却只推阿里的人，是最危险的方向。
_EXCLUDE_MARKERS = ("不考虑", "不接受", "不招", "排除", "不要", "勿投", "不看", "杜绝", "谢绝")
# 「或」为组内 OR 连接词。
_OR_TOKENS = ("或", "或者", "、", "/", ",", "，", ";")

# 公司背景的触发词：句子里出现这些词，才说明点名的公司是「背景要求」而不是顺带提及。
# 只认「背景 / 工作经历 / 经历」会漏掉「有阿里经验」「曾在字节任职」「阿里出身」这些常见写法。
# 判定范围是**局部语句**（见 _split_statements），所以「有电商经验，了解阿里」不会被连坐。
_COMPANY_TRIGGERS = ("背景", "经历", "经验", "任职", "就职", "出身", "待过", "工作过", "来自")
# 表格型 / 字段型 JD 的行标签（「目标公司：字节或阿里」「公司\t阿里」）不走「背景 / 经历」这类
# 表述，需要单独识别。这类是**弱触发**：它只是一行字段，不是一句明确的背景要求，所以只把公司
# 当加分（default=PLUS）——误判成 MUST 会一卡清空整库，代价远大于少筛一层。
_COMPANY_LABEL_TRIGGERS = (
    "目标公司", "公司背景", "任职公司", "公司经历", "过往公司", "现公司", "雇主", "公司",
)
# 公司名后紧跟这些字说明说的是产品/技术栈而不是公司（阿里云、腾讯云、华为云、百度云…）。
_COMPANY_PRODUCT_SUFFIXES = ("云",)

# 年限表达：年限**不属于** exact_constraints 的 kind，它是独立的硬窗口
# （``[n-1, 2n]``，见 ``match/service._years_window``），所以要单独抽取。
# 年数用 \d{1,2} 而非 \d+：否则「2020-2023 年」这类区间会被当成 2020 年经验，
# 撞上 min_years 的 0~80 校验直接把保存打成 422。中文数字同样常见（「五年以上」）。
_YEARS_UNLIMITED = (
    "经验不限", "不限经验", "无经验要求", "经验无要求", "不要求经验", "无需经验",
    "经验均可", "有无经验均可",
)
# 「应届 / 校招 / 刚毕业」说明门槛很低，但**常与年限并提**（「5 年经验，优秀毕业生亦可」），
# 所以只在没有任何数字年限时才当作「不设窗口」，避免把明写的年限抹掉。
_YEARS_FRESH = ("应届", "校招", "刚毕业", "毕业生")
_YEARS_TOKEN = r"(\d{1,2}|[一二三四五六七八九十两]{1,3})"
_YEARS_PATTERNS = (
    re.compile(rf"(?:至少|最少|不低于|起码|怎么也得|最低|大于|超过)\s*{_YEARS_TOKEN}\s*年"),
    re.compile(rf"{_YEARS_TOKEN}\s*[-~～至]\s*(?:\d{{1,2}}|[一二三四五六七八九十两]{{1,3}})\s*年"),
    re.compile(rf"{_YEARS_TOKEN}\s*年\s*(?:及)?以上"),
    re.compile(rf"{_YEARS_TOKEN}\s*年\s*(?:左右|上下)"),
    re.compile(rf"{_YEARS_TOKEN}\s*年\s*\+"),
    re.compile(rf"(?:工作)?年限\s*[:：]?\s*{_YEARS_TOKEN}\s*年"),
    re.compile(rf"{_YEARS_TOKEN}\s*年(?:以上)?(?:工作)?(?:相关)?经验"),
    # 英文写法（外企 / 海归 JD）：「at least 3 years」「5+ years」「3-5 years」「5 years or more」
    re.compile(rf"(?:at least|minimum|min\.?|over|more than)\s*{_YEARS_TOKEN}\s*years?"),
    re.compile(rf"{_YEARS_TOKEN}\s*\+\s*years?"),
    re.compile(rf"{_YEARS_TOKEN}\s*years?\s*(?:or more|\+)"),
    re.compile(rf"{_YEARS_TOKEN}\s*[-~]\s*\d{{1,2}}\s*years?"),
)
# 中文数字 → 阿拉伯数字（只覆盖 1~99：「五年」「十五年以上」「二十五年」）。
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _to_years(token: str) -> float | None:
    """把年限片段换成数字；越界（<=0 或 >60）返回 None，避免污染 min_years 的校验域。"""
    if token.isdigit():
        value = int(token)
    elif token == "十":
        value = 10
    elif token.startswith("十"):
        value = 10 + _CN_DIGITS.get(token[1:], 0)
    elif "十" in token:
        head, _, tail = token.partition("十")
        value = _CN_DIGITS.get(head, 0) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    else:
        value = _CN_DIGITS.get(token, 0)
    return float(value) if 0 < value <= 60 else None


@dataclass(frozen=True, slots=True)
class ExactConstraint:
    kind: str          # school_level | company_history | skill | industry | other_keyword
    operator: str      # OR | AND
    alternatives: tuple[str, ...]
    strength: str      # MUST | PLUS | EXCLUDE
    source: str        # manual | inferred
    source_text: str


@dataclass(frozen=True, slots=True)
class ProfileRequirements:
    """画像文本解析出的要求：硬条件 + 年限（一次模型调用同时产出，零额外调用）。

    ``years_stated`` 把「画像没提年限」与「画像明确不限」区分开：前者要**保留**既有
    ``min_years``，后者要**清空**——两者都表现为 ``min_years is None``，不能只看值。
    """

    constraints: list[dict]
    min_years: float | None
    years_stated: bool


# 合法取值域：与匹配侧 evaluate_exact_constraints 的分派一一对应。
_KINDS = frozenset({"school_level", "degree", "company_history", "skill", "industry", "other_keyword"})
_OPERATORS = frozenset({"OR", "AND"})
_STRENGTHS = frozenset({"MUST", "PLUS", "EXCLUDE"})
# 供 API 层（JD 字段编辑校验）复用的公开契约：只此一份，避免各抄一份导致漂移
# （曾经 api/jd.py 的私有副本缺 degree，保存画像时会被 422 拦掉）。
CONSTRAINT_KINDS = _KINDS
CONSTRAINT_STRENGTHS = _STRENGTHS
# 合并多路结果时的强度优先级（见 merge_requirements）。
_STRENGTH_RANK = {"PLUS": 0, "MUST": 1}

# 「只做软条件」的 kind：skill / other_keyword 是模型最容易过度指定的两类
# （「Google Vertex AI SDK」「5年以上」这种长短语），一旦成为 MUST，AND 判定下只要无人命中
# 就把**整库**清空（2026-09-20 实测 5 个 JD 全部归零）。故这两类的 MUST 一律降级为 PLUS：
# 只参与排序加权（match/service.py 的 preference 分量）与 AI 逐条复核，不淘汰人。
# 保留淘汰力的只有客观、可比对、误判代价可控的三类：school_level / degree / company_history，
# 以及明确写了行业门槛的 industry。
SOFT_ONLY_KINDS = frozenset({"skill", "other_keyword"})

# industry 额外要求：source_text 里必须出现「明确要求」措辞才允许保留 MUST。
# 它是模型最容易从**岗位职责**里误抽的一类：实测同一份 JD 两次导入，一次产出
# ``industry MUST ['支付']``、一次不产出，而「支付」只出现在职责句「负责支付核心链路的服务
# 设计与开发」里。行业硬卡不满足会把整库清空，而用户口径是「泛化背景（互联网/金融）不卡」——
# 随机出现的行业 MUST 必须被压成加分。
_INDUSTRY_MUST_MARKERS = ("必须", "硬性", "必备", "缺一不可", "卡", "及以上")

# 硬条件字段口径：JD 解析提示词与「按文本重解析硬条件」共用同一份，避免两处漂移。
CONSTRAINT_FIELD_SPEC = (
    "exact_constraints：显式硬条件数组，每项含 "
    "kind（school_level | degree | company_history | skill | industry | other_keyword）、"
    "operator（OR | AND，同一项内多个 alternatives 之间的关系，默认 OR）、"
    "alternatives（字符串数组，命中任一即算满足）、"
    "strength（MUST | PLUS | EXCLUDE）、"
    "source（固定 inferred）、"
    "source_text（依据的原文片段，逐字复制，不要改写）。"
    "kind 取值口径："
    "school_level 只放**学校档次**（985 / 211 / 双一流）；"
    "degree 放**学历门槛**（大专 / 本科 / 硕士 / 博士）——学历不要写成 school_level；"
    "company_history 放**原文点名的具体公司**（如 字节、阿里巴巴、腾讯、Google、微软、Oracle、Amazon），"
    "一个 OR 组放同一语句里并列的公司；"
    "「互联网背景 / 金融背景 / 大厂 / 一线互联网」这类**泛化背景不得产出 company_history 约束**"
    "（无具体公司名时不要产出该 kind）；"
    "skill / industry / other_keyword 同原有口径。"
    "判定口径（严格遵守，MUST 误判会直接淘汰候选人）："
    "① 只有原文明确写「必须 / 硬性要求 / 必要条件 / 缺一不可 / 必备 / 卡 / 及以上」才算 MUST，其余一律 PLUS；"
    "② 按单句判定：「985 优先，必须熟悉 Java」里 985 是 PLUS、Java 才是 MUST；"
    "③ 岗位职责、泛能力描述、行业罗列不产出约束；"
    "④ 「985 本科及以上」要产出**两条**约束：school_level(985) + degree(本科)；"
    "⑤ EXCLUDE 只用于原文明确排除项（如「不考虑外包背景」）；"
    "⑥ 没有明确硬条件时输出空数组，不要凑数。"
)

# 年限口径：**只**用于「按文本重解析要求」提示词（`CONSTRAINT_PARSE_PROMPT`）。
# 不并入 `CONSTRAINT_FIELD_SPEC`——后者被 JD 解析提示词与 JD 画像双形态提示词共用，
# 而那两个的输出结构里并没有 min_years 字段。
YEAR_FIELD_SPEC = (
    "min_years：画像里写明的**最低工作年限**，只输出数字（可为小数），不要带「年」字。"
    "「3 年以上」「至少 3 年」「3-5 年」→ 3；「5 年左右」→ 5；"
    "「经验不限 / 不要求经验 / 应届」→ 0（0 表示**不设年限要求**，不是在库里筛 0 年经验）；"
    "画像**没有提到年限**时输出 null——不要按岗位级别、职级或行业惯例推测。"
)

CONSTRAINT_PARSE_PROMPT = """你是招聘系统的要求抽取器，负责从 JD 候选人画像要求里抽取显式硬条件与年限要求。

{spec}

{year_spec}

补充规则：
- 只依据给定文本判断，不引入外部常识、不补全未写明的条件；
- 画像文本通常是一段寻访口径，只有其中写明「必须/卡/必备」或明确排除的才是硬条件；
- 只输出 JSON 对象，不要 markdown 代码块或任何多余文字。

画像要求文本：
{source_text}"""


def industry_requirement_is_explicit(source_text: str) -> bool:
    """行业要求是否写明了「必须 / 硬性 / 及以上」这类措辞。

    写侧（``normalize_constraints`` 降级）与读侧（``match/service._must_constraints`` 过滤）
    共用这一份判定，避免两边口径漂移。
    """
    return any(marker in (source_text or "") for marker in _INDUSTRY_MUST_MARKERS)


def normalize_constraints(raw) -> list[dict]:
    """把模型或人工给出的硬条件规整为可落库的契约形态。

    宁缺毋滥：kind/strength/operator 非法的整条丢弃（匹配侧拿到含义不明的约束会静默失效）；
    **MUST 会被降级为 PLUS 的两种情形** ——
    ① 没有 `source_text` 原文依据（依据不足时不能让它具备淘汰力）；
    ② kind 属于 ``SOFT_ONLY_KINDS``（skill / other_keyword）——即使写了「必须」，也不允许
       具备淘汰力，只按命中率参与排序。这是 2026-09-20「一句话清空全库」事故的代码层止血。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: list[dict] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in raw:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        strength = str(item.get("strength") or "").strip().upper()
        operator = str(item.get("operator") or "OR").strip().upper() or "OR"
        if kind not in _KINDS or strength not in _STRENGTHS or operator not in _OPERATORS:
            continue
        alternatives = tuple(
            dict.fromkeys(
                str(value).strip() for value in (item.get("alternatives") or []) if str(value).strip()
            )
        )
        if not alternatives:
            continue
        source_text = str(item.get("source_text") or "").strip()
        if strength == "MUST" and (not source_text or kind in SOFT_ONLY_KINDS):
            strength = "PLUS"
        elif (strength == "MUST" and kind == "industry"
              and not industry_requirement_is_explicit(source_text)):
            # 行业要求从「岗位职责」里误抽出来时会随机变成硬条件，一律退回加分。
            strength = "PLUS"
        key = (kind, strength, tuple(a.casefold() for a in alternatives))
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "kind": kind,
            "operator": operator,
            "alternatives": list(alternatives),
            "strength": strength,
            "source": str(item.get("source") or "inferred"),
            "source_text": source_text,
        })
    return normalized


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(m.casefold() in folded for m in markers)


def _split_alternatives(text: str) -> list[str]:
    """按 OR 连接词切分为备选列表，去空去重保序。"""
    parts = re.split(r"[或,，、;/]", text)
    return [p.strip() for p in parts if p.strip()]


def _split_statements(text: str) -> list[str]:
    """按句读/分号/逗号切分为局部语句，使 MUST/PLUS 判定只作用于同一语句。"""
    return [s.strip() for s in re.split(r"[。！？；;，,\n]", text) if s.strip()]


def _excludes_mention(clause: str, mention: str) -> bool:
    """否定词是否作用于该**提及本身**，还是作用于它的补集。

    - 「不考虑阿里背景」「排除字节背景」：否定词在提及**之前** → 真的排除它；
    - 「本科以下勿投」「非 985 勿投」「985/211 勿投」「211 以下不考虑」：否定词在提及**之后**
      （否定的是「以下 / 非」这个补集）→ 等价于「必须本科及以上 / 必须 985 或 211」，
      **绝不能**判成排除——判成排除会正好把要招的人全排除，是比漏抽危险得多的错误方向。
    """
    index = clause.find(mention)
    if index <= 0:
        return False
    return _contains_any(clause[:index], _EXCLUDE_MARKERS)


def _strength_for(clause: str, default: str, mention: str | None = None) -> str:
    """在单个局部语句内判定 EXCLUDE / MUST / PLUS；无触发词时用 default。

    **判定顺序是排除 → 优先 → 必须**：

    - 先判排除，且必须**作用域正确**（见 ``_excludes_mention``）：只看「这句话里有否定词」会把
      「本科以下勿投」判成排除本科；
    - 再判 PLUS：门槛词（「及以上/以上」）与优先词可能同句出现，如「本科以上优先」应判 PLUS；
    - 最后才判 MUST。
    """
    if mention is not None and _excludes_mention(clause, mention):
        return "EXCLUDE"
    if _contains_any(clause, _PLUS_MARKERS):
        return "PLUS"
    if _contains_any(clause, _MUST_MARKERS):
        return "MUST"
    return default


def _extraction_names(canonical: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    """抽取阶段可用的公司名写法：canonical + 中文别名 + **无歧义**英文名。

    英文别名（oracle / aws / apple）在 JD 里常常指技术栈或产品而不是公司
    （「Oracle 数据库」「AWS 经验」「Apple Pay」），拿它们抽取会产出错误的 company_history；
    这类写法只在**判定**阶段用于对上候选人公司文本（见 ``_company_hit``）。
    但 ``bytedance`` / ``alibaba`` / ``tencent`` 这类**只会指公司**的英文名要抽，
    否则海归候选人或外企 JD 里的公司背景会整条漏掉。
    """
    return (
        canonical,
        *(alias for alias in aliases
          if not alias.isascii() or alias.casefold() in _UNIQUE_ENGLISH_COMPANIES),
    )


def _company_mentioned(statement: str, name: str) -> bool:
    """公司名是否真的指公司：排除「阿里云 / 腾讯云」这类产品写法（「阿里系」仍算背景）。

    没有这层排除，「有容器化经验，熟悉阿里云 ACK」会被抽成 MUST 的阿里背景硬条件，
    一卡就把整库清空——这是比漏抽更危险的错误方向。英文名按大小写不敏感匹配
    （JD 里 bytedance / ByteDance / BYTEDANCE 都出现）。
    """
    for match in re.finditer(re.escape(name), statement, re.IGNORECASE):
        if statement[match.end():match.end() + 1] in _COMPANY_PRODUCT_SUFFIXES:
            continue
        return True
    return False


def parse_exact_constraints(text: str | None, *, source: str = "manual") -> list[ExactConstraint]:
    """从一段画像文本解析出显式硬条件（纯函数，不依赖模型）。

    按局部语句判定 MUST/PLUS：例如「985优先，必须熟悉Java」中 985 是 PLUS（优先），
    不会因后句的「必须」被升级为 MUST；「字节或阿里背景优先」是 PLUS。没有明确
    触发词时不产出约束，避免把普通描述误升级为硬条件。
    """
    if not text:
        return []
    result: list[ExactConstraint] = []

    for statement in _split_statements(text):
        # 英文别名（bachelor / master degree / phd）按大小写不敏感匹配；中文别名不受影响。
        folded_statement = statement.casefold()
        # 学校档次：同一语句内的多个档次**合并为一条 OR 约束**。
        # 「必须 985 或 211」若拆成两条独立 MUST，判定时会把 OR 当成 AND，211 的人被误拒。
        levels = [canonical for canonical, aliases in _SCHOOL_LEVEL_ALIASES.items()
                  if any(re.search(rf"(?:卡|必须|要求|优先|加分|更佳|最好)?\s*{re.escape(alias)}", statement)
                         for alias in aliases)]
        if levels:
            result.append(ExactConstraint(
                kind="school_level", operator="OR", alternatives=tuple(levels),
                strength=_strength_for(statement, default="PLUS", mention=levels[0]),
                source=source, source_text=statement,
            ))

        # 学历门槛：「本科及以上」「硕士」等 → kind=degree，与学校档次是两个独立维度
        # （「985 本科及以上」= 学校档次约束 AND 学历约束，与合同的组合语义一致）。
        degrees = [canonical for canonical, aliases in _DEGREE_ALIASES.items()
                   if any(alias.casefold() in folded_statement for alias in aliases)]
        if degrees:
            result.append(ExactConstraint(
                kind="degree", operator="OR", alternatives=tuple(degrees),
                strength=_strength_for(statement, default="PLUS", mention=degrees[0]),
                source=source, source_text=statement,
            ))

        # 公司历史：提取「字节或阿里背景」「有阿里经验」「曾在字节任职」等（同一语句内并为 OR 组）。
        # 别名表只放**同义写法**，因此这里连同 canonical 名一起匹配（「字节」不在别名里也能命中）。
        if any(trigger in statement for trigger in _COMPANY_TRIGGERS) or \
                any(trigger in statement for trigger in _COMPANY_LABEL_TRIGGERS):
            companies: list[str] = []
            for canonical, aliases in _COMPANY_ALIASES.items():
                if any(_company_mentioned(statement, name)
                       for name in _extraction_names(canonical, aliases)):
                    companies.append(canonical)
            if companies:
                # 强触发（背景/经历/任职…）默认 MUST；表格行标签是弱触发，默认只作加分。
                strong = any(trigger in statement for trigger in _COMPANY_TRIGGERS)
                strength = _strength_for(
                    statement, default="MUST" if strong else "PLUS",
                    mention=next((c for c in companies if _excludes_mention(statement, c)), None),
                )
                result.append(ExactConstraint(
                    kind="company_history", operator="OR", alternatives=tuple(companies),
                    strength=strength, source=source, source_text=statement,
                ))

        # 技能优先：LangGraph 优先 → PLUS；技能必须 → MUST。
        for match in re.finditer(r"([A-Za-z][\w\s+#./-]{1,30}?)\s*(优先|加分|更佳|必须|必备)", statement):
            # 中文 \\w 会被贪婪吃掉连接词（「CI/CD 为加分」→「CI/CD 为」），去掉结尾虚词，
            # 否则这些脏词会进 exact_constraints 并出现在「符合点」里。
            skill = re.sub(r"(为|的|是|等|及|和|与|、)+$", "", match.group(1)).strip()
            # 只收 ASCII 技能词：正则本就要求以字母开头，而 `\w` 在 Python 里也匹配中文，
            # 于是「Alibaba 或 Tencent 背景优先」会被整句当成技能词（噪声）。
            if not skill or not skill.isascii():
                continue
            strength = _strength_for(statement, default="PLUS", mention=skill)
            result.append(ExactConstraint(
                kind="skill", operator="OR", alternatives=(skill,),
                strength=strength, source=source, source_text=match.group(0),
            ))

    # 去重（同 kind + alternatives 视为同一约束）。
    seen: set[tuple] = set()
    deduped: list[ExactConstraint] = []
    for constraint in result:
        key = (constraint.kind, constraint.operator, tuple(a.casefold() for a in constraint.alternatives))
        if key not in seen:
            seen.add(key)
            deduped.append(constraint)
    return deduped


def parse_years_requirement(text: str | None) -> tuple[bool, float | None]:
    """从画像文本抽取年限要求，返回 ``(是否明确提及, 最低年限)``。

    - ``(False, None)``：画像没提年限 → 调用方**保留**原有 ``min_years``（不要抹掉 JD 解析值）；
    - ``(True, n)``：画像要求 n 年以上，硬窗口仍由 ``_years_window`` 统一算 ``[n-1, 2n]``；
    - ``(True, None)``：画像明确「经验不限」→ 应清空年限要求。

    纯规则抽取，不额外调用模型：保存画像属于高频操作，而年限表达高度模式化
    （「n 年以上 / 至少 n 年 / n-m 年 / n 年左右 / n 年+ / 工作年限：n 年 / 五年以上 / 经验不限」）。
    """
    if not text:
        return False, None
    for marker in _YEARS_UNLIMITED:
        if marker in text:
            return True, None
    for pattern in _YEARS_PATTERNS:
        match = pattern.search(text)
        if match:
            years = _to_years(match.group(1))
            if years is not None:
                return True, years
    # 数字年限优先于「应届 / 校招」：「5 年经验，优秀毕业生亦可」不能把 5 年抹掉。
    for marker in _YEARS_FRESH:
        if marker in text:
            return True, None
    return False, None


def interpret_model_years(raw: float | None) -> tuple[bool, float | None]:
    """把模型给出的 ``min_years`` 归一为 ``(是否明确提及, 最低年限)``，与规则抽取同口径。

    模型契约里 ``0`` 表示「经验不限」，但**不能直接落库**：``_years_window(0)`` 会算出
    ``1~3`` 年的窗口，等于把「不限」变成「只要 1~3 年」。这里把它翻成 ``(True, None)``。
    ``None`` 表示画像没提年限 → ``(False, None)``，调用方保留原值。
    """
    if raw is None:
        return False, None
    value = float(raw)
    if value <= 0:
        return True, None
    return True, value


def merge_rule_constraints(
    model_constraints,
    text: str | None,
) -> tuple[list[dict], float | None]:
    """把规则抽出的硬条件并入**模型结果**，并回传规则读到的年限。供 JD 导入链路使用。

    JD 导入此前只跑模型：同一段 JD 两次解析，模型有时给出 degree、有时不给——明写的
    「本科及以上」「5 年以上」会随模型抖动而丢失（实测同一份多段 JD，两次导入一次有 degree、
    一次没有）。这里用规则兜住底线，与画像链路（``POST /api/jd/parse-constraints``）同一套
    「模型 ∪ 规则」口径。

    返回 ``（合并后的约束 dict 列表, 规则年限或 None）``。**在这里不构造模型对象**：规则层用
    dataclass、模型层（``jd/structured.ExactConstraint``）用 pydantic，而 ``structured`` 已经
    import 本模块，反向 import 会成环——由调用方完成转换。
    """
    model = normalize_constraints([
        item.model_dump() if hasattr(item, "model_dump") else item
        for item in (model_constraints or [])
    ])
    if not text or not text.strip():
        return model, None
    rule = normalize_constraints([
        asdict(item) for item in parse_exact_constraints(text, source="inferred")
    ])
    stated, years = parse_years_requirement(text)
    return merge_requirements(model, rule), (years if stated else None)


def evaluate_exact_constraints(
    constraints: list[ExactConstraint],
    candidate_parsed: dict,
) -> tuple[list[str], list[str]]:
    """返回 (未满足的 MUST 描述, 满足的 PLUS 描述)。

    只有 MUST 未满足才会硬拒；PLUS 仅用于加分排序，不硬拒。
    """
    unmet_must: list[str] = []
    met_plus: list[str] = []
    for constraint in constraints:
        hit = _constraint_hit(constraint, candidate_parsed)
        if constraint.strength == "MUST" and not hit:
            unmet_must.append(constraint.source_text or " or ".join(constraint.alternatives))
        elif constraint.strength == "PLUS" and hit:
            met_plus.append(constraint.source_text or " or ".join(constraint.alternatives))
    return unmet_must, met_plus


def merge_requirements(*sources: list[dict]) -> list[dict]:
    """把多路解析结果并成一份（模型 ∪ 规则），返回 normalize 后的契约列表。

    为什么必须并集、而不是「模型非空就用模型」：两路读的是同一段文本，**各自的漏抽**才是常态。
    实测模型会把「本科及以上学历，5 年以上经验，具备阿里或字节背景」整段判成空数组（只给出
    年限），此时若直接采信模型，画像里明写的学历与公司背景会被静默丢掉——这正是「同样写法
    换个语序就抽不出来」的来源。规则侧对这类**格式固定的客观条件**（学历 / 学校档次 / 点名公司
    / 年限）反而更稳，两路互补。

    并集在判定语义上是安全的：``alternatives`` 是 OR，并集只会**放宽**判定，
    不会凭空把某个候选人挡在门外。

    合并口径：

    - 同 kind 的 ``alternatives`` 取并集，先传入的排在前面（展示顺序，模型优先）；
    - 强度取**最强**的一档（MUST > PLUS）；MUST 仍须带 ``source_text``，由 normalize 兜底降级；
    - 一旦发生合并，``operator`` 归一为 OR（并集语义，不能把两组的 AND 叠起来）；
    - ``source_text`` 保留各路依据并用「；」拼接，界面上能看出「为什么算硬条件」；
    - ``EXCLUDE`` 语义与前两者相反，**不参与并集**，原样保留。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    excluded: list[dict] = []
    for source in sources:
        for raw in source or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip()
            strength = str(raw.get("strength") or "").strip().upper()
            alternatives = [str(a).strip() for a in (raw.get("alternatives") or []) if str(a).strip()]
            if kind not in _KINDS or strength not in _STRENGTHS or not alternatives:
                continue
            if strength == "EXCLUDE":
                excluded.append({**raw, "kind": kind, "alternatives": alternatives})
                continue
            entry = merged.get(kind)
            if entry is None:
                merged[kind] = {
                    "kind": kind,
                    "operator": str(raw.get("operator") or "OR").strip().upper() or "OR",
                    "alternatives": alternatives,
                    "strength": strength,
                    "source": str(raw.get("source") or "inferred"),
                    "source_text": str(raw.get("source_text") or "").strip(),
                }
                order.append(kind)
                continue
            seen = {a.casefold() for a in entry["alternatives"]}
            for alternative in alternatives:
                if alternative.casefold() not in seen:
                    seen.add(alternative.casefold())
                    entry["alternatives"].append(alternative)
            entry["operator"] = "OR"
            if _STRENGTH_RANK.get(strength, 0) > _STRENGTH_RANK.get(entry["strength"], 0):
                entry["strength"] = strength
            basis = str(raw.get("source_text") or "").strip()
            if basis and basis not in entry["source_text"]:
                entry["source_text"] = f"{entry['source_text']}；{basis}".strip("；")

    # 同一 kind 上出现矛盾时以**排除**为准（模型说「必须阿里」、规则说「排除阿里」这类）。
    # 两者并存会让准入门槛与排除项的交集为空——谁都不满足，整库被拒，是并集带来的新风险。
    for entry in excluded:
        survivor = merged.get(entry["kind"])
        if survivor is None:
            continue
        blocked = {a.casefold() for a in entry["alternatives"]}
        remaining = [a for a in survivor["alternatives"] if a.casefold() not in blocked]
        if remaining:
            survivor["alternatives"] = remaining
        else:
            merged.pop(entry["kind"], None)
            order.remove(entry["kind"])

    return normalize_constraints([merged[kind] for kind in order] + excluded)


def exclusion_hits(
    constraints: list[ExactConstraint],
    candidate_parsed: dict,
) -> list[str]:
    """命中 EXCLUDE 项的原文依据（命中即应淘汰该候选人）。

    ``EXCLUDE`` 此前只是被解析出来、从没被判定使用——``evaluate_exact_constraints`` 只分派
    MUST / PLUS，于是「不考虑外包背景」这句话写了等于没写（模型产出了 EXCLUDE、AI 复核清单里
    也标了「排除」，确定性资格层却完全忽略）。这里补上淘汰判定，与 MUST 未满足同一条通道。
    """
    return [
        c.source_text or " or ".join(c.alternatives)
        for c in constraints
        if c.strength == "EXCLUDE" and _constraint_hit(c, candidate_parsed)
    ]


def preference_ratio(
    constraints: list[ExactConstraint],
    candidate_parsed: dict,
) -> float | None:
    """PLUS 项命中率（命中的 PLUS 数 / PLUS 总数）；没有任何 PLUS 项时返回 ``None``。

    降级后的 skill / other_keyword 靠这个值继续起作用：命中率越高排得越前，但不淘汰任何人。
    返回 ``None`` 而非 ``0.0``，是为了让「该 JD 根本没有优先项」与「有优先项但一个都没命中」
    在打分时区分开——前者不参与加权归一，后者是实打实的扣分。
    """
    plus = [c for c in constraints if c.strength == "PLUS"]
    if not plus:
        return None
    met = sum(1 for c in plus if _constraint_hit(c, candidate_parsed))
    return met / len(plus)


def _constraint_hit(constraint: ExactConstraint, candidate_parsed: dict) -> bool:
    alternatives = [a.casefold() for a in constraint.alternatives]
    if constraint.kind == "school_level":
        return _school_level_hit(alternatives, candidate_parsed)
    if constraint.kind == "degree":
        return _degree_hit(constraint.alternatives, candidate_parsed)
    if constraint.kind == "company_history":
        return _company_hit(alternatives, candidate_parsed)
    # skill / industry / other_keyword：词法命中。
    text = _candidate_lexical_text(candidate_parsed)
    return any(a in text for a in alternatives)


def _degree_hit(degrees: tuple[str, ...] | list[str], candidate_parsed: dict) -> bool:
    """学历门槛：候选人最高学历达到**任一**要求即算满足（alternatives 为 OR）。

    只看 ``highest_degree`` —— 与检索层下推的 ``highest_degree IN degrees_at_least(...)``
    同源；若这里改用各段教育经历兜底，会出现「检索层已把人滤掉、事后判定却说通过」的两套口径。
    """
    current = normalize_degree(candidate_parsed.get("highest_degree"))
    if not current:
        return False
    for degree in degrees:
        required = normalize_degree(str(degree))
        if required and current in degrees_at_least(required):
            return True
    return False


def _school_level_hit(levels: list[str], candidate_parsed: dict) -> bool:
    """学校档次判定（**宽松口径**，与检索层 ``array_has_any(school_tags, ...)`` 同源）：

    任一段教育经历（本/硕/博）的 ``school_tags`` 命中任一档次即可，不要求与学历门槛同一段；
    学历门槛由独立的 ``degree`` 约束负责 —— 所以「985 本科及以上」= 两条约束同时成立：
    ① 任一段是 985；② 最高学历 ≥ 本科。（两种口径的人数差异见方案 §12：511 vs 354。）
    """
    school_level = str(candidate_parsed.get("school_level") or "").casefold()
    school_tier = str(candidate_parsed.get("school_tier") or "").casefold()
    tags: set[str] = set()
    for edu in candidate_parsed.get("educations") or []:
        if isinstance(edu, dict):
            tags.update(str(t).casefold() for t in (edu.get("school_tags") or []))
    degree = str(candidate_parsed.get("highest_degree") or "").casefold()
    for level in levels:
        if level in (school_level, school_tier) or level in tags:
            return True
        # 存量兼容：旧解析会把「硕士/博士」误写进 school_level，这里按学历兜底判定，
        # 避免那条约束变成「永不命中的 MUST」把整库拒掉。
        if level in ("硕士", "博士") and level in degree:
            return True
    return False


def _company_names_for_judging(company: str) -> tuple[str, ...]:
    """把约束里的公司写法展开成**同一别名组**的全部写法，用于判定候选人公司文本。

    模型常直接抄原文全称（约束里是「字节跳动」），而候选人简历里可能只写「字节」。
    只做 canonical → 别名的单向展开会漏判，而漏判在 MUST 下等同于误拒。
    """
    key = company.casefold()
    names = [company]
    for canonical, aliases in _COMPANY_ALIASES.items():
        group = (canonical, *aliases)
        if any(key == name.casefold() or key in name.casefold() or name.casefold() in key
               for name in group):
            names.extend(group)
            break
    return tuple(dict.fromkeys(names))


def _company_hit(companies: list[str], candidate_parsed: dict) -> bool:
    parts = [str(candidate_parsed.get("current_company") or "")]
    for exp in candidate_parsed.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("company") or ""))
    company_text = " ".join(p for p in parts if p).casefold()
    for company in companies:
        for name in _company_names_for_judging(company):
            if name.casefold() in company_text:
                return True
    return False


def _candidate_lexical_text(candidate_parsed: dict) -> str:
    parts = [
        str(candidate_parsed.get("summary") or ""),
        " ".join(str(s) for s in (candidate_parsed.get("skills") or [])),
    ]
    for exp in candidate_parsed.get("experiences") or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("summary") or ""))
            parts.append(str(exp.get("title") or ""))
    for proj in candidate_parsed.get("projects") or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("summary") or ""))
            parts.append(str(proj.get("tech_stack") or ""))
    return " ".join(parts).casefold()
