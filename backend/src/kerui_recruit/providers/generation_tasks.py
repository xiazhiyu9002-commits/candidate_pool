"""供应商无关的简历/JD 解析任务：承载当前实施时的完整提示词。

提示词与当前 ``ParsedResume``/``ParsedJd`` 结构（含多值方向字段）保持一致。
这些类只依赖 ``TaskGenerationClient``，不再依赖供应商名或模型名。

职业方向/业务方向词表统一由 ``direction.policy`` 渲染，不在提示词里另抄一份，
避免词表与校验口径漂移。

画像字段（``ai_profile_summary`` / ``candidate_profile``）的口径不在此处另立一套，
统一引用 ``providers.profile_spec``，与独立画像生成器保持同一骨架、同一形态、同一字数。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from kerui_recruit.direction.policy import (
    render_business_taxonomy,
    render_career_taxonomy,
)
from kerui_recruit.jd.profile_constraints import CONSTRAINT_FIELD_SPEC
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.ai.task_client import TaskGenerationClient
from kerui_recruit.resumes.structured import ParsedResume

_CAREER_TAXONOMY = render_career_taxonomy()
_BUSINESS_TAXONOMY = render_business_taxonomy()

_RESUME_PARSE_PROMPT = """你是资深招聘顾问，负责把简历原文解析为结构化 JSON。

输出一个 JSON 对象，字段如下：
- name：候选人真实姓名。按下列顺序提取，命中即用：① 简历开头/顶部的姓名栏、标题，或联系方式（电话、邮箱）附近的姓名；② 带称谓的写法（如「张三先生」「李女士」「Ms. Wang」）取称谓前的姓名；③ 英文名/拼音名（如「Jessica Chen」「Zhang San」）保留原文写法。只输出人名本身，去掉「姓名：」「Name:」等标签前缀与称谓；禁止用文件名、岗位名、公司名、学校或技能代替姓名；简历几乎不会完全没有姓名线索，确实一处都找不到时才填 null，不得臆造或复制文件名；
- total_years：总工作年限，数字。按最早一段工作的起始年月到"至今"（当前时间）计算总年数，四舍五入到 0.5 年（如 2020.07 至今约 6.0）；
- highest_degree：最高学历（博士/硕士/本科/大专）。可从"本科/学士/硕士/博士"等字样，或从入学与毕业年份（如 2016-2020 通常为本科）推断；
- birth_year：出生年份数字，从"出生年月/出生日期"提取，无法判断填 null；
- age：年龄数字，仅当简历明确写出年龄时提取，无法判断填 null；
- gender：性别，从"男/女"等字样提取，只填 男 或 女，无法判断填 null；
- location：现居地/所在地城市；
- preferred_location：期望工作城市，无法判断填 null；
- school：最高学历对应的毕业院校名称；
- school_level：学校等级，取 985 / 211 / 双一流 / 普通 / 海外 之一，无法判断填 null；
- qs_rank：QS 排名数字，无法确定填 null；
- graduation_year：本科毕业年份数字；无本科时填最高学历毕业年份；
- educations：教育经历数组，每项含 school（学校名）、degree（博士/硕士/本科/大专）、major（专业）、graduation_year（该段毕业年份）、country_region（国家或地区，海外学校填写）、school_tags（学校标签数组，如 ["985","211"] 或 ["双一流"]，无法判断填空数组）、qs_year（QS 排名年份数字）、qs_rank（QS 排名数字）。用于支持"北京大学硕士、清华大学本科"这类多段教育；
- industry：候选人主要行业（如 互联网/金融/制造）；
- current_industry：最近一份工作所属行业；
- longest_industry：任职年限最长的那份工作所属行业；
- skills：具体技能词数组，每个元素是单一简短 token（编程语言、框架、中间件、数据库、云原生组件等），如 Java、Spring Boot、Kubernetes、Redis；禁止把「技能+场景」合并（"Spring Boot/Cloud微服务架构"应拆为 Spring Boot、Spring Cloud、微服务）；禁止填"数据结构、操作系统"这类课程名或软技能；
- summary：用一两句话概括候选人的核心工作方向与专长（基于工作经历和项目提炼，不要照抄自我评价）；
- experiences：工作经历数组，每项含 company（公司名）、title（职位）、start_date（起始时间，如 2020.07）、end_date（结束时间，在职填"至今"）、location（该段工作所在城市，无法判断填 null）、industry（该段工作所属行业）、summary（该段工作做了什么，可概括多个项目点）；
- projects：项目经历数组，每项含 name（项目名）、tech_stack（技术栈，如 Go/微服务）、business_scene（业务场景一句话）、summary（项目职责或成果）；
- ai_profile_summary：{candidate_inline_rule}；
- ai_profile_points：画像分点数组，每项含 text（从 ai_profile_summary 中**逐字截取**的原句或原句连续片段，覆盖其中最核心的 3~5 条；不得改写、不得新增、不得调整语序）与 evidence_paths（证据路径数组，如 ["experiences[0].summary"] / ["projects[0].summary"] / ["skills"]，没有证据填空数组）；
- ai_profile_compact：{compact_length}浓缩，概括画像最核心的定位与方向，与 ai_profile_summary 事实一致；
- career_directions：职业方向**大类**数组（最多 2 个），只能从下方「职业方向词表」里的大类代码中选择；有技术证据时至少 1 个，只有完全无法判断时才输出空数组；第一个元素即主方向；
- career_specializations：职业方向**细分**数组，每个已选大类下最多 2 个，只能选该大类下列出的细分代码；无证据不产出，不要为了凑数填满；
- business_directions：**业务方向**数组（最多 2 个），只能从下方「业务方向词表」中选择；只依据项目与工作经历中的业务场景判断，禁止依据公司名或职位名；无证据输出空数组；
- direction_assessment：职业方向评估对象，字段：primary（主方向，必须等于 career_directions 的第一个元素）、secondary（次方向，可空；与主方向不同且证据次强时给出）、confidence（high/medium/low；仅有技能或职位名等单一来源时不得 high）、evidence_paths（证据路径数组，如 ["experiences[0].summary","projects[0].summary"]，只写字段路径不复制正文）、management（true/false，是否有明显团队管理/招聘/绩效/预算等职责）、taxonomy_version（固定 "4"）；管理经历与技术专业并存时 primary 仍写技术专业、management 为 true；

规则：
- 优先从原文提取，允许对年限、学历、行业做合理推断；
- 同一段工作下的多个项目点，应拆成独立的 projects 条目，同时 experiences 的 summary 概括该段整体；
- skills、summary、experiences、projects 是判定解析质量的关键字段：凡原文有证据就必须完整、规范输出，不得因内容较多而省略、合并或只写笼统概括；
- ai_profile_summary 与 ai_profile_points 是同一画像的两种形态，必须同源同事实：先写好 ai_profile_summary 这一段，分点必须逐字取自该段（原句或原句连续片段），不得改写、不得新增，也不得遗漏该段里的关键事实；
- 未出现且无法推断的字段填 null，方向/技能/经历/教育列表可为空数组；
- 只输出 JSON 对象，不要输出 markdown 代码块或任何多余文字。

职业方向判定规则（严格，宁缺毋滥）：
- 证据优先级：项目/工作经历里的**实质交付** > 工作职责 > 技能与工具 > 职位名称。**禁止只凭职位名称判定方向**；职位名只在其他证据缺失时作参考，且不得单独据此判定；
- 每个大类与细分都必须有职责或项目证据支撑。技术名词堆砌、公司名、学历、AI 分类标签都不能单独决定方向；
- 「算」与「不算」的边界示例：只列了 React/Java 等技术栈**不算**全栈交付（需职责/项目里同时有前端与后端的实现类交付）；只写"了解/关注大模型"**不算** AI 应用集成（需 Agent/RAG/模型 API 与业务系统结合的项目产出）；模型训练、微调、算法优化属于 ALGORITHM，**不得**标为 AI 应用集成；数据平台/数据管道的服务端开发属 BACKEND，用 SQL 做数仓/ETL/分析属 DATA；
- 命中数量超过上限时，按证据强度排序，只保留最强的 2 个大类、每个大类下最强的 2 个细分、最强的 2 个业务方向。**放宽数量不等于放宽标准**，准入门槛与只有一个方向时完全相同；不要为了凑数填满；
- 职责相当、信息不足或相互冲突时，选择证据最强的一个方向；OTHER 仅用于明确属于非技术方向（销售/行政/财务/法务等）。

职业方向词表（大类 -> 可选细分）：
{career_taxonomy}

业务方向词表（代码（中文））：
{business_taxonomy}

业务方向判定口径：
- 只统计项目与工作经历中的业务场景（如 projects.business_scene / projects.summary / experiences.summary / experiences.industry），**不统计公司名、职位名、行业标签字面**；
- **不要把「客户行业罗列」当成自己的业务**：如果原文只是泛泛列出服务过的行业（如"客户覆盖汽车、通信等多个行业"），不构成业务方向；只有当该项目确实是候选人**主要交付的业务场景**时才算；
- **通用系统名词不足以单独判定**：订单、商品、会员、核心系统、账务、合规、健康、设备管理这类词在各行业通用，必须同时有该行业的专属证据（如保险的理赔保单、物流的仓储运配、汽车的整车车联网）才能产出该项；
- 若命中多个，取「最近一段工作经历」（end_date 为"至今"或最新的一段）与「任职时长最长的一段工作经历」各自业务证据最强的 1 个；两者相同则只取 1 个；
- 完全无业务场景证据时输出空数组，不要勉强归类；无法归入任何已列业务方向但有明确业务场景时用 OTHER。

{resume_block}"""

_JD_PARSE_PROMPT = """你是资深招聘顾问，负责把 JD 解析为可检索结构。

任务：根据 JD 原文输出 JSON 对象，字段如下：
- title：岗位名称；
- company：公司名（原文未提填空字符串）；
- department：部门（可空）；
- location：工作地点（可空）；
- salary：薪资范围（可空）；
- ai_category：AI 分类，三选一 CORE_AI（职责核心是模型/算法/训练/推理/LLM/数据科学）、AI_RELATED（与 AI 产品协作但非核心）、NON_AI（其他）；
- industry：行业；
- min_years：最低相关年限，数字（无法判断填 null）；
- highest_degree：最低学历（博士/硕士/本科/大专）；
- qs_level：QS 排名等级要求，如 前50/前100/前200/不限（无法判断填 null）；
- core_duties：核心职责数组，每条写成"动词+宾语"的短句（如"负责推荐系统召回"）；只有原文明确给出指标时才带上指标（如"提升点击率20%"），不要为了显得具体而编造或推算数字；
- required_skills：核心必备技能数组，只放「缺失则无法胜任本岗位」的硬技能，最多 3~5 个；每个元素是单一简短技能词（如 Java 开发岗填 [Java, Spring]，Avaloq 岗填 [Avaloq, Avaloq scripting, SQL, PL/SQL]）；「精通 Java/Python/Go 之一」这类必须写成单个 OR 技能（如 "Java or Python or Go"），不要拆成多个独立必备技能；禁止把加分项或泛技术方向（云平台、DevOps、CI/CD、机器学习等）当必备技能；禁止超过 3 个单词的长短语；
- must_skill_groups：必备技能 AND/OR 组数组，组间为 AND、组内 alternatives 为 OR；每项含 alternatives（技能词数组，可互相替代的技能放同一组）与 source_quote（原文中对应原句，用于解释）；例如「熟悉 LangGraph 或 CrewAI」写成 {{"alternatives":["LangGraph","CrewAI"],"source_quote":"熟悉 LangGraph 或 CrewAI"}}；只有原文明确表达为必须（缺失则无法胜任）的技能才进组，工具示例、可替代框架、泛能力一律放 plus_skills；组内词不能为空、不能重复，最多 3~5 组；
- plus_skills：加分技能数组，放「有则更优、但非必备」的技能词（如 Java 岗的 Kubernetes、Docker、云平台、机器学习等），每个元素同样是单一简短技能词；无法判断填空数组；
- plus_industry：加分场景-行业背景数组（如 金融/电商/广告）；
- plus_project_types：加分场景-项目类型数组（如 高并发交易系统/大模型应用）；
- summary：岗位最核心要求一句话，不超过 60 字；
- candidate_profile：{jd_inline_rule}；
- candidate_profile_points：画像分点数组，每项含 text（从 candidate_profile 中**逐字截取**的原句或原句连续片段，覆盖其中最核心的 3~5 条；不得改写、不得新增、不得调整语序）与 evidence_paths（证据路径数组，如 ["core_duties[0]"] / ["required_skills[0]"] / ["requirements[0]"]，没有证据填空数组）；
- candidate_profile_compact：{compact_length}浓缩，概括最核心的寻访口径，与 candidate_profile 事实一致；
- requirements：数组，每项含 kind（MUST 必备 / PLUS 加分 / EXCLUDE 排除）、label（如 技能/学历/行业/地点/证书）、value（具体值）。
- {constraint_spec}
- career_directions：职业方向**大类**数组（最多 3 个），只能从下方「职业方向词表」里的大类代码中选择；至少 1 个；第一个元素即主方向；**「优先项/重点项/核心职责」对应的方向必须纳入**；
- career_specializations：职业方向**细分**数组，每个已选大类下最多 2 个，只能选该大类下列出的细分代码；无证据不产出；
- business_directions：**业务方向**数组（最多 2 个），只能从下方「业务方向词表」中选择；依据岗位名称、职责描述里的业务细节、项目/业务方向判断；无法判断时输出空数组；
- direction_assessment：职业方向评估对象，字段：primary（主方向，必须等于 career_directions 的第一个元素）、secondary（次方向，可空；与主方向不同且证据次强时给出）、confidence（high/medium/low；仅有技能或职位名等单一来源时不得 high）、evidence_paths（证据路径数组，如 ["core_duties[0]","required_skills[0]"]，只写字段路径不复制正文）、management（true/false，是否有明显团队管理/招聘/绩效/预算等职责）、taxonomy_version（固定 "4"）；管理经历与技术专业并存时 primary 仍写技术专业、management 为 true；

规则：必须/优先/排除分别识别，不能把加分当必须；required_skills、core_duties、requirements、candidate_profile 是判定解析质量的关键字段，凡 JD 原文有证据就必须完整、规范输出；没有证据填 null 或空；candidate_profile 与 candidate_profile_points 是同一画像的两种形态，必须同源同事实：先写好 candidate_profile 这一段，分点必须逐字取自该段（原句或原句连续片段），不得改写、不得新增，也不得遗漏该段里的关键事实；只输出 JSON 对象，不要 markdown。

职业方向判定规则（严格，宁缺毋滥）：
- 证据优先级：**「优先项 / 重点项 / 核心职责 / 必须具备」对应的方向必须纳入** > 一般职责描述 > 技能列表 > 职位名称。**禁止只凭职位名称判定方向**；职位名只在其他证据缺失时作参考，不得单独据此判定；
- 「优先项、重点项」属于本次重点：它们指向的职业细分与业务方向必须被收录；其余泛化描述（公司介绍、福利、泛能力要求）可以降低权重甚至忽略；
- 「算」与「不算」的边界示例：只列了 React/Java 等技术栈**不算**全栈交付；只写"了解大模型"**不算** AI 应用集成（需 Agent/RAG/模型 API 与业务系统结合）；模型训练、微调、算法优化属于 ALGORITHM，**不得**标为 AI 应用集成；
- 命中数量超过上限时，按证据强度排序取前 N 个；**放宽数量不等于放宽标准**，准入门槛与只有一个方向时完全相同。

职业方向词表（大类 -> 可选细分）：
{career_taxonomy}

业务方向词表（代码（中文））：
{business_taxonomy}

业务方向判定口径：
- 依据岗位名称、职责描述里的业务细节、项目/业务方向判断，**不看公司名**；
- **不要把「客户行业罗列」当成岗位业务**：泛泛列出服务过的行业不构成业务方向，只有当该业务确实是岗位**主要交付的场景**时才算；
- **通用系统名词不足以单独判定**：订单、商品、会员、核心系统、账务、合规、健康、设备管理这类词在各行业通用，必须有该行业的专属证据才能产出该项；
- 无法判断时输出空数组，不要勉强归类；无法归入任何已列业务方向但有明确业务场景时用 OTHER。

{jd_block}"""

_JD_SPLIT_PROMPT = """你是招聘系统，负责把一段可能包含多个岗位的 JD 文本拆分成独立的 JD。

任务：把原文中每一个独立岗位的 JD 拆出来，按顺序返回一个 JSON 对象：
{{"chunks": ["<第一个 JD 的原文>", "<第二个 JD 的原文>", ...]}}

规则：
- 原文可能是纯文本、或从 Word/Excel 提取出的文字，可能包含一个或多个岗位；
- 每个 chunk 只保留该岗位相关的原文（可含标题/职责/要求等），不要改写、不要总结、不要遗漏；
- 如果原文只有一个岗位，返回仅含一个元素的数组；
- 只输出 JSON 对象，不要 markdown 代码块或任何多余文字。

JD 原文：
{jd_text}"""


class JdSplit(BaseModel):
    chunks: list[str] = Field(default_factory=list)


# 送模型的正文上限（字符）。简历/JD 原文此前是**整段原样**拼进提示词：一份几十页的
# 扫描件 OCR 结果或超长 JD 会直接把请求顶爆（上游按「context length / too long」拒绝，
# 映射为 E_API_INPUT），或被上游静默截断在中间、丢掉尾部关键信息。
# 超限时按「保头 + 保尾 + 中段省略」截断，并在正文里明确声明省略了多少字符。
_MAX_MODEL_INPUT_CHARS = 20000
_HEAD_RATIO = 0.7


def truncate_model_input(text: str, *, limit: int = _MAX_MODEL_INPUT_CHARS) -> str:
    """把送模型的正文限制在上限内：保头 + 保尾，中段以显式省略标记替代。

    保头多于保尾：简历的姓名/联系方式/概述与最近的经历都在前部，尾部多是更早的经历。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * _HEAD_RATIO)
    tail = limit - head
    omitted = len(text) - limit
    return (
        f"{text[:head]}\n\n"
        f"……（此处省略 {omitted} 个字符，原文过长）……\n\n"
        f"{text[-tail:]}"
    )


def _resume_prompt_kwargs() -> dict[str, str]:
    """简历解析提示词的占位符取值（文本解析与视觉解析共用，避免口径漂移）。"""
    return {
        "candidate_inline_rule": profile_spec.CANDIDATE_INLINE_RULE,
        "compact_length": profile_spec.COMPACT_LENGTH,
        "career_taxonomy": _CAREER_TAXONOMY,
        "business_taxonomy": _BUSINESS_TAXONOMY,
    }


def _jd_prompt_kwargs() -> dict[str, str]:
    """JD 解析提示词的占位符取值（文本解析与视觉解析共用，避免口径漂移）。"""
    return {
        "jd_inline_rule": profile_spec.JD_INLINE_RULE,
        "compact_length": profile_spec.COMPACT_LENGTH,
        "career_taxonomy": _CAREER_TAXONOMY,
        "business_taxonomy": _BUSINESS_TAXONOMY,
        "constraint_spec": CONSTRAINT_FIELD_SPEC,
    }


def render_resume_parse_prompt(resume_text: str) -> str:
    """文本简历解析提示词：末尾输入段是简历正文（超长时按上限截断）。"""
    return _RESUME_PARSE_PROMPT.format(
        **_resume_prompt_kwargs(),
        resume_block=f"简历原文：\n{truncate_model_input(resume_text)}",
    )


def render_resume_vision_parse_prompt(image_instruction: str) -> str:
    """视觉简历解析提示词：末尾输入段换成图片指令，其余口径与文本解析完全一致。

    之前视觉链路直接拿未格式化的模板发出去，模型收到的是字面量占位符
    （``{career_taxonomy}`` 等），方向词表与字数口径全部丢失。
    """
    return _RESUME_PARSE_PROMPT.format(**_resume_prompt_kwargs(), resume_block=image_instruction)


def render_jd_parse_prompt(jd_text: str) -> str:
    """文本 JD 解析提示词：末尾输入段是 JD 正文（超长时按上限截断）。"""
    return _JD_PARSE_PROMPT.format(
        **_jd_prompt_kwargs(), jd_block=f"JD 原文：\n{truncate_model_input(jd_text)}"
    )


def render_jd_vision_parse_prompt(image_instruction: str) -> str:
    """视觉 JD 解析提示词：末尾输入段换成图片指令，其余口径与文本解析完全一致。"""
    return _JD_PARSE_PROMPT.format(**_jd_prompt_kwargs(), jd_block=image_instruction)


class AiResumeParser:
    def __init__(self, client: TaskGenerationClient) -> None:
        self._client = client

    async def parse_resume(self, text: str) -> ParsedResume:
        return await self._client.complete_json(
            messages=[{"role": "user", "content": render_resume_parse_prompt(text)}],
            response_model=ParsedResume,
        )


class AiJdParser:
    def __init__(self, client: TaskGenerationClient) -> None:
        self._client = client

    async def parse_jd(self, text: str) -> ParsedJd:
        return await self._client.complete_json(
            messages=[{"role": "user", "content": render_jd_parse_prompt(text)}],
            response_model=ParsedJd,
        )

    async def split_jds(self, text: str) -> list[str]:
        result = await self._client.complete_json(
            messages=[{"role": "user", "content": _JD_SPLIT_PROMPT.format(jd_text=text)}],
            response_model=JdSplit,
        )
        chunks = [chunk.strip() for chunk in result.chunks if chunk.strip()]
        return chunks or [text.strip()]
