"""画像规范唯一来源：同一字段只允许有一套骨架、形态与字数口径。

存在两条产出画像的路径，它们写的是同一批字段：
1. 解析期内联产出（``providers/generation_tasks.py`` 的 ``ai_profile_summary`` /
   ``candidate_profile``）；
2. 独立生成器（``resumes/profile.py``、``jd/profile.py`` 的单文本与双形态 JSON）。

两条路径必须引用本模块，否则同一字段会被两套口径分别产出，造成事实与字数漂移。
"""
from __future__ import annotations

import re

from kerui_recruit.jd.profile_constraints import CONSTRAINT_FIELD_SPEC

# 字数口径：两侧各自区间（候选人履历信息量大于寻访口径），但同一字段只允许一个数字。
# 所有产出该字段的提示词（独立生成器单文本/双形态、解析期内联）都必须引用这里的常量。
CANDIDATE_NARRATIVE_LENGTH = "120~160 字"
JD_NARRATIVE_LENGTH = "80~150 字"
COMPACT_LENGTH = "40~80 字"

# 画像**重写**（定点修正）的输出上界。取值是「安全网」而不是「约束」：
# 上限那侧是 160 字，中文按 1~1.5 token/字估算约 240 token，取 1024 留了约 4 倍余量——
# 合规输出永远不会被截断，而彻底失控的长篇输出会被兜住。
#
# 为什么只给文本类调用设上界：实测（2026-09-22）输出长度是延迟的主导因素
# （同模型「只回一句」2.6~5.4 秒 vs 不约束的文本 73 秒），但**结构化调用不能设**——
# 截断 JSON 会变成 `E_API_SCHEMA`，比慢更糟，那边由 schema 校验兜。
PROFILE_REWRITE_MAX_TOKENS = 1024

# 双形态契约：整体段落、分点、浓缩与关键事实必须同源，不得增删或改义。
DUAL_FORM_CONTRACT = f"""输出形态（必须同时满足，narrative 与 points / facts 必须来自同一事实源，不得增删或改义）：
- narrative：完整整体段落，必须整段、不得换行、不得分点，只输出这一段正文。先把这一段写好、写到位：它是第一产物，也是被检索与阅读的正文；
- points：分点数组，覆盖画像中最核心的 3~5 条。**每条必须是 narrative 中逐字出现的原句或原句的连续片段**——直接截取，不得改写、不得新增、不得调整语序、不得换同义词；分点只是把同一段话里最核心的几句挑出来展示；
  分点粒度固定为**句子**：一条 = 一个完整句子（以「。」结尾），**不得把一个句子按逗号、顿号或分号拆成多条**。
  「此前任职众安保险」「长期深耕金融科技」这类短语片段不是合格分点——它们缺少主语或谓语，单独成行读不通；请把它们所在的那一整句作为一条。
- compact：{COMPACT_LENGTH}浓缩，概括最核心的定位与方向；
- facts：关键事实清单，同样只能逐字取自 narrative 或 points，不得改写；facts 同样受本字段的禁写清单约束；
- evidence_paths：证据路径数组，指向结构化字段（如 experiences[0].summary / projects[0].summary /
  skills / core_duties[0] / required_skills[0]），没有证据时留空数组，不要编造。"""

# 量化口径（两侧对称）：只允许「段位类」数字，其余一律不写。
# 段位类 = 直接说明这个人「管多大的事」：业务/用户/客户/资金/资产/商业体量、管理团队人数。
# 只说明「做得细不细」的数字（效能指标、过程计数、相对提升、奖项次数）一律改写成能力或结果表述。
QUANTIFICATION_RULE = (
    "量化口径：只允许出现「段位类」数字——业务/用户/客户/资金/资产规模、营收/ARR/GMV 等商业体量、"
    "管理团队人数，它们直接说明这个人管多大的事（如「支撑百万级用户业务」「带 50 人团队」「覆盖百亿资产」）。"
    "以下数字一律不写：技术效能指标（响应时间、QPS/吞吐、可用性百分比、KS/AUC/FPD 等模型指标）、"
    "过程计数（表数、调度任务数、接口数、设备数、文档数）、相对提升比例（提升 X%、降本 Y%、效率提升 Z%）、"
    "奖项/专利/论文的次数，以及其他只说明「做得细不细」的技术细节数字；"
    "需要表达这些内容时改写成能力或结果表述，例如「核心接口响应时间从 250ms 降至 80ms」→"
    "「主导核心接口性能治理与容量保障」，「FPD30 从 4% 压降至 1.6%」→「主导反欺诈体系搭建，显著压低首逾水平」。"
    "段位类数字必须有证据，不得用行业惯例估算或推断。"
)

# 增量修正规则（两侧对称）：单一候选人画像与 JD 画像共用同一套修正语义。
INCREMENTAL_RULES = """当提供了「上一版画像」或「用户额外要求」时，按以下规则增量修正：
1. 用户要求优先于上一版画像：用户明确提出的新增、删除、调整、放宽、提高或降低权重的要求，必须体现。
2. 用户未提及的内容默认保留：不要因为局部修改而重新生成整套画像，也不要删除上一版中未被否定的有效信息。
3. 用户要求可能是在调整权重，而不一定增加新条件。例如「Java 更重要」→ 提升 Java 权重；「前端有基础即可」→ 降低前端要求；「Python 不重要」→ 降级或移除；「主要看外资行和金融科技」→ 行业背景提升为核心维度；「做交易系统的更重要」→ 提升交易系统业务经验权重；「需要带团队」→ 加入团队管理/Tech Lead。
4. 不得过度推导：只能使用上一版画像、结构化证据和用户要求中已有的信息，不得自行新增行业、公司类型、技术栈、工作年限、学历、职级、业务场景、管理要求、产品经验。
5. 调整权重时必须真正改变画像中的排序和权重，而不是简单追加一句说明。例如上一版「要求 Java、React、Python、Go 开发经验」+ 用户要求「主要看 Java，前端有基础即可，Python 和 Go 不重要」→ 改为「以 Java 为核心技术栈，具备基础前端/React 能力即可；Python、Go 不作为核心筛选条件」。
6. 用户要求与上一版画像冲突时，以用户最新要求为准。
7. 只输出修正后的完整画像，不要输出修改说明或差异对比。"""

# 候选人画像禁写清单：独立生成器与解析期内联必须共用同一份，否则两条路径会产出两套口径。
CANDIDATE_FORBIDDEN = (
    "以下内容一律不出现在画像里："
    "① 学历、学校、QS 排名、在职深造与学习方向（如「已录取非全日制硕士」「系统学习大模型核心技术」）；"
    "② 非核心卖点的头衔标签（如「管培生」；而资深架构师、大厂高职级这类有含金量的身份可以写）；"
    "③ 技术效能数字、过程计数（如「等 4 个 AI 项目」「迁移 1600+ 应用」「跨 9 个团队」）、相对提升比例、奖项次数（见量化口径）；"
    "④ 与核心卖点无关的经历、公司或模块清单；"
    "⑤ 具体项目名与技术实现的细节展开、更细的工程栈（Kubernetes/DevOps/CI 工具链、中间件清单、模型服务与向量库组件等）——画像是概括，不是复述简历。"
)

# 语言与收尾约束：两侧产出该字段的提示词共用，避免「评价式收尾」反复出现。
CANDIDATE_LANGUAGE_RULE = (
    "语言要求：信息密度高、专业、客观、克制；用「主导/负责/构建/设计/落地/聚焦/深耕」等与事实匹配的表达；"
    "不得虚构年限、管理经验、技术能力、业务成果、学历、论文、专利、客户、项目规模，证据不足的信息不写。"
    "禁止评价式收尾：不要以「兼具…」「擅长…」「复合优势在于…」「能打通…全链路」这类没有具体事实支撑的句子作为最后一句。"
)

_CANDIDATE_RULES = f"""硬指标（不满足即视为不合格，必须重新组织内容，而不是超写）：
- 长度必须落在 {CANDIDATE_NARRATIVE_LENGTH}（按中文字符计）；4~5 句，每句不超过 35 字；写完先自行核对字数，超了就删句，不许超写；
- 每句一个信息点，用「。」分隔；整段不得分点、不得加标题、不得换行；
- 最后一句必须落在事实上（做过什么系统、带过多大团队）。
  反例（末句是空泛评价，不合格）：「…在野村负责 Prime Services 交易与风险平台后端功能并承担 L3 支持。兼具交易系统开发与跨境机构协作经验。」
  正例（以事实收尾）：「…在野村负责 Prime Services 交易与风险平台后端功能并承担 L3 支持。」
  完整合格示例（约 150 字，可直接照这个体量写）：「约 9.5 年 Java 低延迟后端开发经验，现任摩根士丹利高级开发工程师，此前任职汇丰、野村。长期深耕投资银行全球市场交易与风险平台，覆盖外汇期权、大宗商品、证券借贷等业务。技术主线为 Java/Spring Boot 低延迟后端与高吞吐事件驱动集成，兼具 C++ 与 JVM 性能优化能力。在野村负责 Prime Services 交易与风险平台后端功能并承担 L3 支持。」

生成口径（先在心里判断核心卖点是什么、哪些经历与卖点无关，再按下面 5 个部分落笔，顺序即正文顺序）：
1. 【定位】「X年XX领域经验 + 核心职业定位」，并写出知名公司/机构与有含金量的职级身份（如摩根士丹利、腾讯财付通、资深架构师、大厂高职级）；年限只能按时间线计算或证据明确描述，不得夸大；学历、学校、深造与学习方向一律不写。
2. 【行业与业务背景】长期所处的行业与业务环境 + 主要交付的业务场景（交易、财富管理、支付、风控、广告、供应链等），只写证据支持的内容。
3. 【技术主线与代表经历】技术主线写到「主干 + 关键栈」为止，最多再点 2 个最能体现差异化能力的技术名词；Kubernetes/DevOps/CI 工具链、中间件清单、模型服务与向量库组件、DDD/状态机等更细的实现细节一律不写。随后只写最近 1~2 段经历（突出公司、职位、职责与代表项目），更早的经历只用一句带过其行业与方向：知名公司/机构保留原名，不得改写成「某广告技术公司」；**与主线无关的早期通用模块经历（如互联网公司的车辆、票务、邀约、投诉类模块）不写，也不要用它们收尾**；同类信息必须合并。架构师/技术负责人层级要点出沉淀的架构思想或方法论，并说明解决了什么结构性问题。
4. 【职责层级】端到端负责 / 架构设计 / 核心模块负责 / 参与；有技术负责人、团队管理、招聘绩效职责时必须写出来——「带团队」比任何技术效能数字都更能说明段位；只是带教新人、指导实习生的，只写「具备团队管理能力」。证据写「参与」不得改「主导」，「负责模块」不得扩成「整体负责人」。
5. 【量化与禁写】{QUANTIFICATION_RULE}
{CANDIDATE_FORBIDDEN}
{CANDIDATE_LANGUAGE_RULE}

推荐组织方式：用 4~5 句覆盖【定位】→【行业与业务背景】→【技术主线与代表经历】→【职责层级/团队规模】；写入证据里没有的内容即视为不合格。"""

# JD 画像禁写清单：与候选人侧一样，独立生成器与解析期内联必须共用同一份。
JD_FORBIDDEN = (
    "以下内容一律不出现在画像里："
    "① 「硬门槛为…」「我们要找什么样的人」这类元话语；"
    "② 复述岗位名称或 JD 标题本身（如「资深数据工程负责人」「Java 工程师」）：这些词 JD 正文已有，"
    "写进来只是重复占字数；岗位名里若带额外信息（如「中级 java 开发工程师（保险产品方向）」的「保险产品方向」），"
    "只提取该信息并转成实质要求（「具备保险业务经验」）；"
    "③ 无依据的评价性修饰词：资深、优秀、出色、顶尖、杰出、强（作为定语）等——除 JD 原文明确要求对应级别或年限；"
    "④ 任何学科/专业名称或专业方向表述（「计算机」「软件工程」「计算机相关」「相关专业」「理工科背景」等，"
    "即使 JD 原文写了某专业要求也必须整词省略），学历只保留层次与院校层级；"
    "⑤ 全栈/服务端工程师普遍都具备的通用要求（分布式系统、微服务、事件驱动架构、微前端、消息队列、缓存、"
    "数据库、对象存储等），这类要求对非技术招聘人员没有区分度；"
    "⑥ 非重点业务名词（如「再平衡」「风险管理」这类次要要求）；"
    "⑦ 软性与过程性描述（产品意识、跨团队推动、独立推进复杂项目、沟通协调、抗压能力等）；"
    "⑧ 与前文重复的加分项、逐条照抄的职责清单与技能清单、招聘流程与软性套话。"
)

_JD_GOAL = """目标：你是资深猎头。先想清楚这个岗位真正要找的是什么样的人——JD 里哪些条件决定成败、哪些只是套话与流程说明；然后写出给同事看的寻访口径，让不懂技术的招聘同事读完就能直接去搜人、判断简历。不要照抄 JD 原句，也不要逐条翻译 JD。
（下面只给必须遵守的边界；哪些条件重要、按什么顺序组织，由你判断。）"""

# 形态与字数两条写法：整段形态（单文本 / 解析期内联）与要点形态（独立生成链路，整段由要点本地拼接）。
# 其余必须遵守项共用同一份，避免两条链路各写一套口径。
_JD_SHAPE_NARRATIVE = (
    f"- 形态与字数：一段 {JD_NARRATIVE_LENGTH}（按中文字符计），3~5 句，不分点、不加标题、不换行；写不下就删次要条件，不许超写；"
)
_JD_SHAPE_POINTS = (
    f"- 形态与字数：合计 {JD_NARRATIVE_LENGTH}（按中文字符计），拆成 3~5 条要点、每条一句话；"
    "系统会按你给出的顺序把要点依次拼成整段，所以要点之间必须顺序连贯、互不重复、也不要写「首先/其次/另外」这类衔接词；"
    "写不下就删次要条件，不许超写；"
)

_JD_MUST_RULES = f"""- 不要复述岗位名称或 JD 标题；岗位名里若带额外信息（如「中级 java 开发工程师（保险产品方向）」），只提取成实质要求（「具备保险业务经验」）；
- 不写没有证据的评价性修饰词（资深/优秀/出色/顶尖/杰出等）；
- 学历只写层次与院校层级（「本科及以上」「硕士优先」），不写学科或专业方向；
- JD 里明确的优先项与加分项必须体现，但不与前文重复；
- 技术栈必须是 JD 里的具体技术名词（Java、ReactJS、Spark…）；「分布式系统/微服务/高并发/服务治理」这类人人都具备的通用能力不许当技术栈写；同类技术只留最核心的一两个，不要逐项铺开；
- 业务方向与业务名词按核心度收敛（订单/执行管理系统这类同义表述合并为「金融交易系统」），只保留决定成败的部分。
- {QUANTIFICATION_RULE}
{JD_FORBIDDEN}"""


def _jd_rules(shape: str) -> str:
    """JD 口径正文：形态一条由调用方给，其余必须遵守项各链路共用。"""
    return f"{_JD_GOAL}\n\n必须遵守：\n{shape}\n{_JD_MUST_RULES}"


_JD_RULES = _jd_rules(_JD_SHAPE_NARRATIVE)
_JD_POINTS_RULES = _jd_rules(_JD_SHAPE_POINTS)


# 参与候选人画像生成的输入字段，同时用于计算稳定的输入哈希与判定画像过期。
CANDIDATE_PROFILE_INPUT_FIELDS = (
    "name", "total_years", "highest_degree", "location", "industry",
    "current_industry", "longest_industry", "skills", "summary",
    "experiences", "projects", "educations", "current_company", "current_title",
)

# 参与 JD 画像生成的输入字段。
JD_PROFILE_INPUT_FIELDS = (
    "title", "company", "department", "location", "industry", "min_years",
    "highest_degree", "required_skills", "summary", "core_duties", "requirements",
)


def instruction_block(instruction: str | None) -> str:
    """用户额外要求区块；无内容时返回空串，保证 prompt 不留空槽。"""
    if not instruction or not instruction.strip():
        return ""
    return f"用户额外要求（需结合到画像中）：\n{instruction.strip()}\n"


def previous_block(previous: str | None) -> str:
    """上一版画像区块；存在时同时给出增量修正规则。"""
    if not previous or not previous.strip():
        return ""
    return (
        f"上一版画像（请在此基础上增量更新，保留仍正确的内容，只按用户要求调整，不要全量重写）：\n"
        f"{previous.strip()}\n\n{INCREMENTAL_RULES}\n"
    )


CANDIDATE_PROFILE_TEMPLATE = f"""你是资深招聘顾问与高级人才画像分析师。根据下面的结构化简历证据，生成一段高信息密度的中文候选人画像摘要，{CANDIDATE_NARRATIVE_LENGTH}。严格只基于证据，输出完整整体的一段话。

{DUAL_FORM_CONTRACT}

{_CANDIDATE_RULES}

{{instruction}}{{previous}}
结构化简历证据：
{{evidence}}"""

CANDIDATE_PAIR_TEMPLATE = f"""你是资深招聘顾问与高级人才画像分析师。根据下面的结构化简历证据，生成候选人画像的结构化结果（facts + narrative + points + compact）。

{_CANDIDATE_RULES}

{DUAL_FORM_CONTRACT}

{{instruction}}{{previous}}
结构化简历证据：
{{evidence}}

只返回 JSON（不要 markdown、不要解释），结构如下：
{{{{"facts":[{{{{"text":"关键事实","evidence_paths":["experiences[0].summary"]}}}}],"narrative":"整体段落（{CANDIDATE_NARRATIVE_LENGTH}）","points":[{{{{"text":"分点一句","evidence_paths":["projects[0].summary"]}}}}],"compact":"{COMPACT_LENGTH}浓缩"}}}}"""

JD_PROFILE_TEMPLATE = f"""你是一名资深猎头顾问。

你的唯一任务是输出 candidate_profile：一段中文（{JD_NARRATIVE_LENGTH}）。这是猎头给 Sourcer 的寻访口径，要让非技术招聘人员能直接据此搜人和判断简历。

只输出 candidate_profile 这一段纯文本。不要输出 JSON、不要分点、不要加标题、不要 Markdown、不要代码块、不要任何解释或寒暄。

{_JD_RULES}

{{instruction}}{{previous}}
结构化 JD 证据：
{{evidence}}"""

JD_POINTS_TEMPLATE = f"""你是一名资深猎头顾问。根据下面的结构化 JD 证据，给出这个岗位的寻访口径：**要点数组 + 硬条件**。
整段画像由系统按你给的要点顺序拼成，你只需要写要点本身，不要另写整段。

{_JD_POINTS_RULES}

{CONSTRAINT_FIELD_SPEC}

{{instruction}}{{previous}}
结构化 JD 证据：
{{evidence}}

只返回 JSON（不要 markdown、不要解释），结构如下：
{{{{"points":[{{{{"text":"要点一句","evidence_paths":["core_duties[0]"]}}}}],"exact_constraints":[{{{{"kind":"skill","operator":"OR","alternatives":["Java"],"strength":"MUST","source":"inferred","source_text":"必须熟悉 Java"}}}}]}}}}"""

# 解析期提示词把画像拆成多个 JSON 字段，无法嵌入多段骨架，这里给出与上面完全同口径的
# 单行浓缩版，保证解析期内联产出的画像与独立生成器一致。
CANDIDATE_INLINE_RULE = (
    f"基于简历原文生成一段高信息密度的中文候选人画像摘要：长度必须落在 {CANDIDATE_NARRATIVE_LENGTH}（按中文字符计），"
    "4~5 句、每句不超过 35 字，用「。」分隔，整段不分点、不加标题、不得换行；最后一句必须落在事实上（做过什么系统、带过多大团队），"
    "不得以「兼具…」「擅长…」「复合优势在于…」「能打通…全链路」这类评价式句子收尾；写不下就删细节，不许超字数。"
    "落笔前先在心里判断这个人的核心卖点是什么、哪些经历与卖点无关，再按以下顺序落笔——"
    "【定位】用「X年XX领域经验 + 核心职业定位」给出定位，并写出知名公司/机构与有含金量的职级身份；年限只能按时间线计算或原文明确描述；"
    "【行业与业务背景】概括长期所处的行业与业务环境及主要业务场景（交易、财富管理、支付、风控、广告、供应链等），只写原文支持的内容；"
    "【技术主线与代表经历】技术主线写到「主干 + 关键栈」为止，最多再点 2 个最能体现差异化的技术名词；Kubernetes/DevOps/CI 工具链、中间件清单、模型服务与向量库组件、DDD/状态机等更细的实现细节一律不写；"
    "架构师/技术负责人层级必须点出其沉淀的架构思想或方法论，并说明解决了什么结构性问题；代表经历只写最近 1~2 段公司与职责，知名公司/机构保留原名（不得改写成「某广告技术公司」），"
    "更早的经历只用一句带过其行业与方向，与主线无关的早期通用模块经历（如互联网公司的车辆、票务、邀约、投诉类模块）不写、也不要用它们收尾，同类信息必须合并；"
    "【职责层级】明确是端到端负责/架构设计/核心模块负责/参与；有技术负责人或团队管理职责时必须写出来，只是带教新人、指导实习生的只写「具备团队管理能力」；"
    "原文写「参与」不得改「主导」，「负责模块」不得扩成「整体负责人」；"
    f"【量化口径】{QUANTIFICATION_RULE}"
    f"【禁写清单】{CANDIDATE_FORBIDDEN}"
    f"【语言约束】{CANDIDATE_LANGUAGE_RULE}"
)

JD_INLINE_RULE = (
    f"先想清楚这个岗位真正需要什么样的人（JD 里哪些条件决定成败、哪些只是套话），再用一段中文写出给同事看的寻访口径，"
    f"让不懂技术的招聘同事读完就能直接搜人、判断简历；不要照抄 JD 原句，也不要逐条翻译 JD。长度 {JD_NARRATIVE_LENGTH}（按中文字符计），"
    "3~5 句、整段不分点、不加标题、不得换行；写不下就删次要条件，不许超字数。"
    "必须遵守：不要复述岗位名称（岗位名带额外信息时只提取成实质要求，如「中级 java 开发工程师（保险产品方向）」→「具备保险业务经验」）；"
    "不写「资深/优秀/出色/顶尖」这类没有证据的修饰词；学历只写层次与院校层级（如「本科及以上」），不写学科或专业方向；"
    "JD 里明确的优先项与加分项必须体现、不与前文重复；技术栈必须是 JD 里的具体技术名词（Java、ReactJS、Spark…），"
    "不得用「分布式系统」「微服务」「高并发」「服务治理」「分库分表」「JVM 性能调优」这类通用能力充当技术栈；"
    "业务名词按核心度收敛（订单/执行管理系统这类同义表述合并为「金融交易系统」），只保留决定成败的部分。"
    f"【量化口径】{QUANTIFICATION_RULE}"
    f"【禁写清单】{JD_FORBIDDEN}"
    "其余（哪些条件重要、怎么组织句子）由你判断；只能基于 JD 原文及其直接逻辑关系归纳，不得新增无依据要求"
)

# ---------------------------------------------------------------------------
# 画像校验与定点重写
# ---------------------------------------------------------------------------
# 提示词里的硬指标是软约束，模型会遵守不到位（实测长度普遍超 20%~40%）。
# 这里做可机检的校验，命中后把「模型自己的原文 + 违规原因」再喂回去做一次定点重写，
# 让字数与禁写清单从「希望模型遵守」变成「产出前必须过闸」。
# 校验只覆盖能可靠匹配的部分（长度、形态、学历词、数字类别、收尾句、JD 技术主线），
# 不做语义判断，因此它是护栏而不是证明。

_VET_EDU = re.compile(
    r"(本科|硕士|博士|大专|专科|985|211|双一流|QS\s*前\s*\d+|QS\s*\d+|非全日制|在职硕士|在职研究生|管培生)")
_VET_NUMBER_BAN = re.compile(
    r"(QPS|TPS|毫秒|\d+\s*ms|可用性\s*\d|KS\s*\d|AUC\s*\d|FPD|准确率\s*\d|召回率\s*\d|"
    r"提升\s*\d+|降低\s*\d+|下降\s*\d+|缩短\s*\d+|减少\s*\d+|压降\s*\d+|"
    r"\d+\s*(%|％|张表|个表|张|个任务|个接口|台设备|人日|篇|次|项专利|个团队|"
    r"个\s*AI\s*项目|个\s*AI\s*应用|个系统|个项目|个应用)|\d+\s*\+\s*应用)")
# 段位类数字（业务/用户/资金/团队规模）在这条画像口径里是允许的，先从文本里剔掉再查禁例。
_VET_NUMBER_ALLOW = re.compile(r"(\d+\s*(亿|万|百万|千万|人|名)|百万级|千万级|亿级|百亿|千亿|十万级)")
_VET_EVAL_CLOSING = ("兼具", "擅长", "复合优势", "能打通", "综合来看", "整体而言")
# JD 侧：没有证据的评价性修饰词与「岗位名称复述」都算浪费字数。
_VET_JD_FILLER_WORDS = ("资深", "优秀", "出色", "顶尖", "杰出")
_VET_JD_GENERIC_TECH = re.compile(
    r"(技术主线|技术栈|技术要求|技术方向)[^。]*(分布式系统|微服务|分库分表|分布式事务|JVM\s*性能调优|高并发架构|服务治理)")


def _length_bounds(spec: str) -> tuple[int, int]:
    """从「120~160 字」这样的口径里取出上下限，避免校验与提示词各写一个数字。"""
    match = re.search(r"(\d+)\s*~\s*(\d+)", spec)
    if not match:
        raise ValueError(f"无法解析画像字数口径：{spec}")
    return int(match.group(1)), int(match.group(2))


def _number_hits(body: str) -> list[str]:
    scrubbed = _VET_NUMBER_ALLOW.sub(" ", body)
    return sorted({match.group(0).strip() for match in _VET_NUMBER_BAN.finditer(scrubbed)})


def vet_profile(text: str, *, side: str) -> tuple[str, ...]:
    """检查画像正文是否满足硬指标，返回违规原因（空元组 = 通过）。

    ``side`` 取 ``candidate``（候选人画像，禁学历）或 ``jd``（岗位画像，保留学历门槛）。
    """
    body = (text or "").strip()
    if not body:
        return ("画像正文为空",)
    issues: list[str] = []
    if "\n" in body:
        issues.append("出现了换行或分点，必须压成一段")
    low, high = _length_bounds(CANDIDATE_NARRATIVE_LENGTH if side == "candidate" else JD_NARRATIVE_LENGTH)
    if len(body) > high:
        issues.append(f"长度 {len(body)} 字，超过上限 {high} 字，请删句压到 {low}~{high} 字")
    elif len(body) < low:
        issues.append(f"长度 {len(body)} 字，不足下限 {low} 字，请补足到 {low}~{high} 字")
    if side == "candidate":
        hit = sorted(set(_VET_EDU.findall(body)))
        if hit:
            issues.append("出现了学历/学校/深造类词（" + "、".join(hit) + "），候选人画像一律不写")
    hit = _number_hits(body)
    if hit:
        issues.append("出现了不该写的数字（" + "、".join(hit[:6]) + "），效能指标、过程计数、提升比例、奖项次数一律不写")
    sentences = [part.strip() for part in body.split("。") if part.strip()]
    if sentences and any(word in sentences[-1] for word in _VET_EVAL_CLOSING):
        issues.append(f"最后一句是评价式收尾（{sentences[-1][:24]}…），必须以事实收尾")
    if side == "jd":
        filler = [word for word in _VET_JD_FILLER_WORDS if word in body]
        if filler:
            issues.append("出现了没有证据的评价性修饰词（" + "、".join(filler) + "），岗位画像不写这类词")
        if _VET_JD_GENERIC_TECH.search(body):
            issues.append("技术栈写成了通用要求，必须换成 JD 里出现的具体技术名词")
    return tuple(issues)


def render_rewrite_instruction(issues: tuple[str, ...], *, side: str, draft: str) -> str:
    """把「模型上一次的原文 + 校验结论」组装成定点修正指令。

    草稿写在指令里，而不是当作 ``previous``（「上一版画像」区块的规则是保留内容、
    不要全量重写，模型会直接把草稿原样返回，等于不修）。
    只涉及字数时按方向分开下指令：**超了只删句、少了只补句**。实测让模型「重写一遍」它
    改不动——超字数照样超、不足下限照样短；给它一个具体动作（删掉一整句 / 补一句）它才做得到。
    """
    subject = "候选人画像" if side == "candidate" else "岗位候选人画像"
    listed = "\n".join(f"{index}. {issue}" for index, issue in enumerate(issues, 1))
    if all("长度" in issue for issue in issues):
        low, high = _length_bounds(CANDIDATE_NARRATIVE_LENGTH if side == "candidate" else JD_NARRATIVE_LENGTH)
        found = re.search(r"长度\s*(\d+)\s*字", " ".join(issues))
        current = int(found.group(1)) if found else 0
        if current > high:
            action = (
                f"这次只做删减：当前 {current} 字，必须删到 {low}~{high} 字之间（需要删掉约 "
                f"{current - high} 字，相当于删掉 1~2 个整句）。"
                "直接整句删除信息量最低的句子（优先删最早或最不关键的一段经历细节），其余句子原样保留；"
                "**含「优先」「加分」的句子不得删除**（它们是寻访必需项）；"
                "不要改写措辞、不要新增内容、不要输出修改说明与分点，只输出删减后的整段画像。"
            )
        else:
            # 不足下限也走「只做一件事」，方向与删减相反：**补**。
            #
            # 这里曾经复用删减分支，于是 `max(current - high, low - current)` 在
            # 「当前 47 字、区间 80~150」时算出 33，指令成了「删掉约 33 字」——而这份文本
            # 总共只有 47 字、目标是补 33 字。实测（2026-09-22 真机，岗位画像快速档）正文
            # 47~62 字，定向重写**怎么都修不动**，根因就是这个反方向的指令。
            action = (
                f"这次只做补充：当前 {current} 字，不足下限 {low} 字，需要补足约 {low - current} 字"
                "（相当于补 1 句）。"
                "只补写**证据里已经存在、但画像没写进去**的条件（JD 里明确写出的必备技能、"
                "加分项、硬性门槛，或简历里已有的代表经历与职责层级），补成一句完整的话接到合适位置；"
                "不得编造证据里没有的事实，不得改写、删除或重排已有的句子，不要把一句话拆成多句；"
                "不要输出修改说明与分点，只输出补充后的整段画像。"
            )
    else:
        action = (
            "请严格保持事实与其余正确内容不变，只修正以上问题；不得新增事实，"
            "不要输出修改说明，不要分点，只输出修正后的整段画像正文。"
        )
    return (
        f"你上一次输出的{subject}未通过校验。上一次的原文如下（仅供你修改，不要原样返回）：\n"
        f"{draft.strip()}\n\n校验发现的问题：\n{listed}\n\n修正要求：{action}"
    )


_VET_LENGTH_OVER = re.compile(r"长度\s*(\d+)\s*字，超过上限\s*(\d+)\s*字")
_VET_LENGTH_UNDER = re.compile(r"长度\s*(\d+)\s*字，不足下限\s*(\d+)\s*字")


def issue_severity(issues: tuple[str, ...]) -> tuple[int, int]:
    """比较两版产出的严重度：先比问题条数，再比**离字数区间的距离**（都越小越好）。

    必须同时认「超过上限」与「不足下限」两个方向。只算超出量的话，不足下限的偏离量恒为 0，
    47 字与 79 字会得到同一个严重度 `(1, 0)`，于是 `produce_pair_with_vet` 里
    `fixed >= best` 成立 —— **一次真的补进了 32 字的重写会被当作「未改善」丢掉**。
    实测（2026-09-22 真机，岗位画像快速档）正文 47~62 字怎么重写都停在原地，这是第二个原因。
    """
    deviation = 0
    for issue in issues:
        over = _VET_LENGTH_OVER.search(issue)
        if over:
            deviation = max(deviation, int(over.group(1)) - int(over.group(2)))
            continue
        under = _VET_LENGTH_UNDER.search(issue)
        if under:
            deviation = max(deviation, int(under.group(2)) - int(under.group(1)))
    return (len(issues), deviation)
