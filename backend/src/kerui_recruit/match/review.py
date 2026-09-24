"""可选 AI 深度复核：prompt 构造、判定流水线与结构化结论解析。

复核只看技术、业务与项目层面（学历、学校层次、年限、公司、城市等硬条件在前置筛选里
已经处理过，不参与复核）：按「项目经历 → 工作经历 → 技术栈」三个维度给结论，输出
推荐理由与风险点两列。

岗位侧输入按来源区分：JD 画像经人工改过（``manual_overrides.candidate_profile``）时用画像
（它就是「技术、业务要求」的口径）；否则用 JD 原文，并按小节标注【岗位职责】【优先项】
【任职要求】，让判据的权重（职责/优先项 > 任职要求）在输入里就可见。

三档结论（recommend / pending / reject）在提示词里有明确判据，并配合确定性兜底：
前置资格被拒直接判不推荐；三段全空却推荐则降级为待核。
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import select

from kerui_recruit.db.models import JdRevision, MatchResult, MatchRun, ResumeRevision
from kerui_recruit.providers.ai.contracts import ReasoningMode

# 并发复核上限，避免单批触发上游限流。
_REVIEW_CONCURRENCY = 5
# 单条复核的调用超时（秒）：一次挂死不能拖住整批；超时条目会显式标记出来。
_REVIEW_CALL_TIMEOUT_SECONDS = 120.0
# JD 原文长度上限：实测现役 JD 中位 767 字、最长 1022 字，正常不会触发；
# 仅作为将来超长 JD 的兜底（按小节裁剪，绝不从句子中间截断）。
_JD_TEXT_MAX_CHARS = 6000
# 小节标题行长度上限：超过就按正文处理，避免把长句误判成标题。
_HEADING_MAX_CHARS = 14

SECTION_DUTIES = "岗位职责"
SECTION_PLUS = "优先项"
SECTION_REQUIREMENTS = "任职要求"
SECTION_OTHER = "其他"

# 小节识别规则：顺序即优先级（先匹配职责/优先项，再要求，最后无关小节）。
_SECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (SECTION_DUTIES, re.compile(
        r"(岗位职责|工作职责|职位职责|工作内容|职位描述|主要职责|职责描述|你将负责|负责的工作|工作范围)")),
    (SECTION_PLUS, re.compile(
        r"(优先项|优先条件|优先考虑|优先录用|加分项|加分条件|nice\s*to\s*have|preferred|具备以下.{0,8}优先)")),
    (SECTION_REQUIREMENTS, re.compile(
        r"(任职要求|任职资格|任职条件|岗位要求|职位要求|资格要求|能力要求|技能要求|我们希望你|你需要具备)")),
    (SECTION_OTHER, re.compile(
        r"(福利|待遇|我们提供|公司介绍|关于我们|团队介绍|投递|应聘|招聘流程|联系方式|薪资|薪酬|办公地点|工作地点|base)")),
)
# 超长裁剪时的丢弃顺序；优先项/加分项永不丢弃。
_DROP_ORDER = (SECTION_OTHER, SECTION_REQUIREMENTS, SECTION_DUTIES)



@dataclass(frozen=True, slots=True)
class JdSection:
    """JD 原文的一个小节：标签用于渲染权重，text 是原文片段（不改写）。"""

    label: str
    text: str


class ReviewVerdictModel(BaseModel):
    """复核结论（匹配复核与搜索复核共用同一份 JSON 契约）。

    三个维度固定顺序：项目 → 工作经历 → 技术栈；不再产出逐条要求清单与结论。
    """

    verdict: str
    project_match: list[str] = []
    experience_match: list[str] = []
    tech_match: list[str] = []
    risks: list[str] = []


# 提示词共用常量：匹配复核与搜索复核引用同一份，避免出现两段平行文本各自漂移。
# 用词保持中性（「要求」「职责与业务」「最低线条件」），两侧只在开头交代「要求」指岗位要求还是搜索条件。
REVIEW_WEIGHT_RULES = (
    "判断权重：职责与业务（要做什么）与优先项/加分项，优先于最低线条件（技能与经验清单）。"
    "职责与业务不匹配时不得推荐，即使最低线条件全部对上；"
    "最低线条件里非必备项缺失不影响推荐，但必须写进风险点；"
    "命中排除项、或「缺失则无法胜任」的必备条件明确缺失时，判为不推荐。"
)
REVIEW_VERDICT_RULES = (
    "推荐（recommend）：项目或工作经历中有具体证据（具体系统、业务场景、本人职责）表明做过与要求同类的事，"
    "且技术栈与技术要求对得上主干；优先项命中至少一项则更强。"
    "待核（pending）：只有技能清单对得上、职责与业务场景没有证据；只能靠「可能/类似」推断；项目描述太粗无法判断业务。"
    "不推荐（reject）：职责与业务方向明确不同且不可迁移；命中排除项或「缺失则无法胜任」的必备条件明确缺失；三个维度都没有实质匹配证据。"
)
REVIEW_OUTPUT_CONTRACT = (
    "只返回 JSON（字段顺序固定）："
    '{"verdict":"recommend|pending|reject",'
    '"project_match":["项目经历与岗位业务/技术要求是否匹配…（projects[0]）"],'
    '"experience_match":["工作经历与岗位业务/技术要求是否匹配…（experiences[1]）"],'
    '"tech_match":["候选人技术栈与岗位技术要求是否匹配…（skills）"],'
    '"risks":["缺口或风险…（experiences[0]）"]}。'
    "三个维度必须按 project_match、experience_match、tech_match 的顺序写，只写真正有依据的内容，没有就填空数组；"
    "每条一句话、直接给结论，不写过渡词与修饰语，也不要写「必备技能」「满足/部分满足」这类清单式描述；"
    "每条结尾用括号标注证据编号；确无证据写「未见证据」，不要臆造经历或技术。"
)


def _heading_label(line: str) -> str | None:
    """判断一行是不是小节标题：返回标签或 None（按正文处理）。"""
    text = line.strip().strip("：: ")
    if not text or len(text) > _HEADING_MAX_CHARS:
        return None
    for label, pattern in _SECTION_RULES:
        if pattern.search(text):
            return label
    return None


def segment_jd_text(source_text: str) -> list[JdSection]:
    """按标题行把 JD 原文切成小节（原文文字不做任何改写）。

    识别不出任何标题时返回单个「其他」小节，渲染时不加标签——避免给模型错误的权重暗示。
    """
    sections: list[JdSection] = []
    current_label = SECTION_OTHER
    buffer: list[str] = []
    recognised = False

    def flush() -> None:
        text = "\n".join(item for item in buffer if item.strip()).strip()
        if text:
            sections.append(JdSection(current_label, text))

    for raw_line in (source_text or "").splitlines():
        line = raw_line.rstrip()
        head, separator, tail = line.partition("：")
        if not separator:
            head, separator, tail = line.partition(":")
        label = _heading_label(head) if separator else _heading_label(line)
        if label is not None:
            flush()
            buffer = [tail.strip()] if tail.strip() else []
            current_label = label
            recognised = True
            continue
        buffer.append(line)
    flush()
    if not recognised:
        body = (source_text or "").strip()
        return [JdSection(SECTION_OTHER, body)] if body else []
    return sections


def render_jd_sections(sections: Sequence[JdSection]) -> str:
    """渲染成带【】标签的岗位要求文本；只有「其他」时不加标签。"""
    if not sections:
        return ""
    if len(sections) == 1 and sections[0].label == SECTION_OTHER:
        return sections[0].text
    parts = [
        section.text if section.label == SECTION_OTHER else f"【{section.label}】{section.text}"
        for section in sections
    ]
    return "\n".join(part for part in parts if part)


def trim_sections(sections: list[JdSection], max_chars: int = _JD_TEXT_MAX_CHARS) -> tuple[list[JdSection], dict | None]:
    """超长兜底裁剪：按小节优先级丢弃，绝不从句子中间截断。

    丢弃顺序：无关小节（福利/公司介绍/投递等）→ 任职要求自末尾往前 → 岗位职责自末尾往前；
    优先项/加分项永不丢弃。返回（裁剪后的小节, 裁剪说明或 None）。
    """
    before = sum(len(section.text) for section in sections)
    if before <= max_chars:
        return sections, None
    kept = list(sections)
    dropped: list[str] = []
    for label in _DROP_ORDER:
        if sum(len(section.text) for section in kept) <= max_chars:
            break
        remaining = [section for section in kept if section.label != label]
        if len(remaining) != len(kept):
            dropped.append(label)
            kept = remaining
    while sum(len(section.text) for section in kept) > max_chars:
        index = next((i for i in range(len(kept) - 1, -1, -1) if kept[i].label != SECTION_PLUS), None)
        if index is None or len(kept[index].text.splitlines()) <= 1:
            break
        lines = kept[index].text.splitlines()
        kept[index] = JdSection(kept[index].label, "\n".join(lines[:-1]).strip())
    kept = [section for section in kept if section.text]
    return kept, {
        "before": before,
        "after": sum(len(section.text) for section in kept),
        "dropped_sections": dropped,
    }


def _build_jd_text(jd_parsed: dict) -> str:
    """兜底：JD 原文缺失时，用结构化解析结果拼出岗位侧文本。"""
    parts: list[str] = []
    if jd_parsed.get("summary"):
        parts.append(f"岗位要求：{jd_parsed['summary']}")
    if jd_parsed.get("candidate_profile"):
        parts.append(f"目标人选：{jd_parsed['candidate_profile']}")
    duties = [str(d) for d in (jd_parsed.get("core_duties") or []) if d]
    if duties:
        parts.append("核心职责：" + "；".join(duties))
    proj_types = [str(p) for p in (jd_parsed.get("plus_project_types") or []) if p]
    if proj_types:
        parts.append("项目类型：" + "、".join(proj_types))
    industries = [str(i) for i in (jd_parsed.get("plus_industry") or []) if i]
    if industries:
        parts.append("行业背景：" + "、".join(industries))
    skills = [str(s) for s in (jd_parsed.get("required_skills") or []) if s]
    plus = [str(s) for s in (jd_parsed.get("plus_skills") or []) if s]
    if skills or plus:
        parts.append("技术栈：" + "、".join((*skills, *plus)))
    return "\n".join(parts) or "（无结构化岗位信息）"


def resolve_jd_source(
    jd_parsed: dict,
    source_text: str | None,
    manual_overrides: dict | None = None,
) -> tuple[str, str, dict | None]:
    """决定复核用的岗位侧文本，返回（文本, 来源标签, 裁剪说明）。

    - 画像经人工改过（``manual_overrides.candidate_profile``）→ 用画像，它本身就是
      「技术、业务要求」的口径；
    - 否则用 JD 原文，并按小节标注【岗位职责】【优先项】【任职要求】，让权重在输入里可见；
    - 原文缺失时退回解析结果（防御，现役数据不会走到）。
    """
    if (manual_overrides or {}).get("candidate_profile"):
        profile = str(jd_parsed.get("candidate_profile") or "").strip()
        if profile:
            return profile, "profile", None
    if (source_text or "").strip():
        sections, trimmed = trim_sections(segment_jd_text(source_text))
        return render_jd_sections(sections), "source_text", trimmed
    return _build_jd_text(jd_parsed), "parsed", None


def build_basis(
    *,
    source: str,
    eligible: bool,
    rejected_by: str | None = None,
    trimmed: dict | None = None,
) -> dict:
    """判据依据（不进 UI）：来源、是否过前置资格、命中的维度、是否被兜底降级、是否裁剪。"""
    return {
        "source": source,
        "eligible": eligible,
        "rejected_by": rejected_by,
        "matched_dimensions": [],
        "degraded": None,
        "trimmed": trimmed,
    }


def _eligibility_reason_text(decision) -> str:
    """把前置资格的拒绝码翻成中文（必要时附缺失技能），用于风险点与 basis。"""
    labels = {
        "career_direction_mismatch": "职业方向与岗位不一致",
        "must_skills_missing": "岗位必备技能未命中",
    }
    parts: list[str] = []
    for reason in getattr(decision, "reasons", ()) or ():
        if reason in labels:
            parts.append(labels[reason])
        elif reason.startswith("exact_constraint:"):
            parts.append(f"硬条件未满足：{reason.split(':', 1)[1]}")
        elif reason.startswith("exclude_constraint:"):
            parts.append(f"命中排除项：{reason.split(':', 1)[1]}")
        else:
            parts.append(reason)
    missing = tuple(getattr(decision, "missing_skills", ()) or ())
    if missing:
        parts.append("缺失技能：" + "、".join(missing[:5]))
    return "；".join(parts) or "前置资格不匹配"


def apply_consistency_fallback(verdict: dict, basis: dict) -> tuple[dict, dict]:
    """自相矛盾就降级（确定性兜底，不依赖模型自述）。

    - 三段全空却给推荐 → 降为待核（``degraded=no_evidence``）；
    - 给不推荐但三段都有实质证据 → 保留结论并标记（``degraded=reject_with_evidence``）。
    """
    if verdict.get("failed"):
        return verdict, basis
    dimensions = [(name, verdict.get(name) or ()) for name in ("project_match", "experience_match", "tech_match")]
    basis = {**basis, "matched_dimensions": [name for name, items in dimensions if items]}
    if verdict.get("verdict") == "recommend" and not basis["matched_dimensions"]:
        return {**verdict, "verdict": "pending"}, {**basis, "degraded": "no_evidence"}
    if verdict.get("verdict") == "reject" and basis["matched_dimensions"]:
        return verdict, {**basis, "degraded": "reject_with_evidence"}
    return verdict, basis



def _failed_verdict(message: str, error: str) -> dict:
    """复核失败的条目：仍按「待核」呈现，但显式带上失败标记与原因。

    以前失败被静默写成 pending，用户看到的是「全是待核、没有结果」，无法区分
    「模型判不了」和「调用压根没成功」。
    """
    return {
        "verdict": "pending",
        "project_match": (),
        "experience_match": (),
        "tech_match": (),
        "risks": (message,),
        "failed": True,
        "error": error,
    }


def build_review_prompt(jd_text: str, candidate_text: str) -> str:
    """构造复核 prompt：给岗位要求与候选人简述，按三个维度输出匹配点与风险点。

    只要求技术、业务与项目层面的匹配判断；每条结论附证据编号，证据缺失时写「未见证据」。
    """
    return (
        "你在复核一个岗位与候选人是否匹配。只看技术、业务与项目层面："
        "学历、学校层次、年限、城市等硬条件已在前面筛过，不要评价也不要复述。\n"
        f"{REVIEW_WEIGHT_RULES}\n"
        f"岗位要求：\n{jd_text}\n"
        f"候选人（含项目/经历证据编号）：\n{candidate_text}\n"
        "按下面三点给结论，顺序固定（没有内容的维度填空数组）：\n"
        "1. 项目：候选人的项目经历与岗位的业务、技术要求是否匹配；\n"
        "2. 工作经历：候选人的工作经历与岗位的业务、技术要求是否匹配；\n"
        "3. 技术栈：候选人的技术栈与岗位的技术要求是否匹配。\n"
        f"{REVIEW_VERDICT_RULES}\n"
        f"{REVIEW_OUTPUT_CONTRACT}"
    )


def build_evidence_section(evidence_pack: dict | None) -> str:
    """把 S4 证据包渲染成 prompt 里的一段（概况 + 技术 / 业务最相关片段）。

    槽位名是 match 专用的 overview/tech/business；搜索侧另用
    overview/primary/complementary，两者不共享槽位命名。
    """
    if not evidence_pack:
        return ""
    labels = (("overview", "整体概况"), ("tech", "技术最相关片段"), ("business", "业务最相关片段"))
    lines = [f"{label}：{evidence_pack[key]}" for key, label in labels if evidence_pack.get(key)]
    if not lines:
        return ""
    return "检索命中的原文片段（可直接引用，编号未知时只写片段内容）：\n" + "\n".join(lines)


def build_candidate_text(data: dict) -> str:
    """候选人简述（含项目/经历证据编号），匹配复核与搜索复核共用同一口径。

    只给技术、业务与项目相关的内容：概括、当前职位与**公司（作为经历背景）**、行业、
    技能、前 5 段工作经历与前 5 个项目。学历、学校层次、QS、年限、年龄、性别、城市、
    期望城市等硬条件在前置筛选里已经处理过，一律不传——复试阶段评价它们只会引入噪声。
    """
    parts: list[str] = []
    if data.get("summary"):
        parts.append(f"概括：{data['summary']}")
    if data.get("current_title") or data.get("current_company"):
        parts.append(f"当前：{data.get('current_title') or ''} @ {data.get('current_company') or ''}".strip())
    if data.get("industry"):
        parts.append(f"行业：{data['industry']}")
    skills = [str(s) for s in (data.get("skills") or []) if s]
    if skills:
        parts.append("技能：" + "、".join(skills))
    for i, exp in enumerate((data.get("experiences") or [])[:5]):
        if not isinstance(exp, dict):
            continue
        head = " @ ".join(str(x) for x in (exp.get("title"), exp.get("company")) if x)
        summary = str(exp.get("summary") or "").strip()
        if head or summary:
            parts.append(f"experiences[{i}]：{head} {summary}".strip())
    for i, proj in enumerate((data.get("projects") or [])[:5]):
        if not isinstance(proj, dict):
            continue
        name = str(proj.get("name") or "").strip()
        business = str(
            proj.get("business_scene") or proj.get("business") or proj.get("domain") or ""
        ).strip()
        tech = proj.get("tech_stack")
        if isinstance(tech, (list, tuple)):
            tech_text = "、".join(str(t) for t in tech)
        else:
            tech_text = str(tech) if tech else ""
        summary = str(proj.get("summary") or "").strip()
        body = "；".join(x for x in (business, tech_text, summary) if x)
        if name or body:
            parts.append(f"projects[{i}]：{name} {body}".strip())
    return "\n".join(parts) or "（无结构化候选人信息）"


class MatchReviewService:
    """对一次匹配 run 的合格配对逐项做 AI 深度复核（异步任务 handler 调用）。"""

    def __init__(self, session_factory, task_client, reasoning_task_client=None) -> None:
        self.session_factory = session_factory
        self.task_client = task_client
        self.reasoning_task_client = reasoning_task_client

    async def review_run(
        self,
        run_id: str,
        report=None,
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> list[dict]:
        """复核这次 run 的**全部**合格配对：匹配到的每一个都要有结论。

        条数与整批时长都不设上限（一次 run 上千条也全跑完）：调用量随配对数线性增长是
        刻意的取舍——「匹配到的人都必须有结论」优先于省调用。长任务靠 worker 每 30 秒
        续租保活，用户可随时「取消复核」；单条调用保留超时（一次挂死不能拖住整批），
        超时/失败的条目显式标记出来。
        """
        # 深度思考用推理模型；否则用快速模型（快速模型不支持 REQUIRED，推理模型不支持 OFF）。
        client = (
            self.reasoning_task_client
            if reasoning == ReasoningMode.REQUIRED and self.reasoning_task_client is not None
            else self.task_client
        )
        with self.session_factory() as session:
            run = session.get(MatchRun, run_id)
            if run is None:
                raise LookupError(f"MatchRun not found: {run_id}")
            # 高分优先：即使中途被取消，最重要的配对也已经出结论。
            results = list(session.scalars(
                select(MatchResult)
                .where(MatchResult.run_id == run_id)
                .order_by(MatchResult.total_score.desc(), MatchResult.id)
            ))
            # 逐条按 result.jd_revision_id（回退 run.jd_revision_id）取 JD 解析数据，
            # 兼容 JD 正向（单 JD）与反向（多 JD）匹配 run。
            jd_revisions: dict[str, JdRevision | None] = {}
            candidate_data: dict[str, dict] = {}
            for result in results:
                jd_rev_id = result.jd_revision_id or run.jd_revision_id
                if jd_rev_id and jd_rev_id not in jd_revisions:
                    jd_revisions[jd_rev_id] = session.get(JdRevision, jd_rev_id)
                revision = session.get(ResumeRevision, result.resume_revision_id) if result.resume_revision_id else None
                candidate_data[result.candidate_id] = (revision.parsed_data or {}) if revision else {}

        from kerui_recruit.match.policy import evaluate_pair

        verdicts_by_index: dict[int, dict] = {}
        pending_ai: list[tuple[int, str, dict]] = []

        for index, result in enumerate(results):
            jd_revision = jd_revisions.get(result.jd_revision_id or run.jd_revision_id)
            jd_parsed = (jd_revision.parsed_data or {}) if jd_revision else {}
            data = candidate_data.get(result.candidate_id, {})
            # 资格被拒绝（方向不一致 / 必需技能缺失 / 命中排除项）的配对不进入 AI 复核：
            # 直接判不推荐并把原因写进风险点，避免「明显不匹配却显示待核」。
            decision = evaluate_pair(jd_parsed, data)
            if decision.eligibility == "rejected":
                reason = _eligibility_reason_text(decision)
                verdicts_by_index[index] = {
                    "verdict": "reject",
                    "project_match": (), "experience_match": (), "tech_match": (),
                    "risks": (f"前置资格未通过（{reason}），已在匹配阶段排除",),
                    "basis": build_basis(source="eligibility", eligible=False, rejected_by=reason),
                }
                continue
            # S4 证据包：把检索命中的原文片段一并给模型，减少「只见摘要、编造经历」。
            evidence_section = build_evidence_section(
                (result.score_breakdown or {}).get("evidence_pack")
            )
            candidate_text = build_candidate_text(data)
            if evidence_section:
                candidate_text = f"{candidate_text}\n{evidence_section}"
            # 岗位侧输入：人工改过的画像优先（它就是技术/业务要求的口径），否则用带小节标注的 JD 原文。
            jd_text, source_label, trimmed = resolve_jd_source(
                jd_parsed,
                jd_revision.source_text if jd_revision else None,
                jd_revision.manual_overrides if jd_revision else None,
            )
            prompt = build_review_prompt(jd_text, candidate_text)
            pending_ai.append(
                (index, prompt, build_basis(source=source_label, eligible=True, trimmed=trimmed)))

        semaphore = asyncio.Semaphore(_REVIEW_CONCURRENCY)
        done = 0

        async def review_one(index: int, prompt: str, basis: dict) -> tuple[int, dict]:
            nonlocal done
            async with semaphore:
                try:
                    model = await asyncio.wait_for(
                        client.complete_json(
                            [{"role": "user", "content": prompt}],
                            ReviewVerdictModel,
                            reasoning=reasoning,
                        ),
                        timeout=_REVIEW_CALL_TIMEOUT_SECONDS,
                    )
                    cleaned, final_basis = apply_consistency_fallback(validated_verdict(model), basis)
                    return index, {**cleaned, "basis": final_basis}
                except asyncio.TimeoutError:
                    return index, {
                        **_failed_verdict(
                            f"复核超时（单条超过 {int(_REVIEW_CALL_TIMEOUT_SECONDS)} 秒）", "TimeoutError"
                        ),
                        "basis": basis,
                    }
                except Exception as error:  # noqa: BLE001 - 单对失败不阻断整批，但必须可见
                    return index, {
                        **_failed_verdict(f"复核失败：{type(error).__name__}", type(error).__name__),
                        "basis": basis,
                    }
                finally:
                    done += 1
                    if report is not None and pending_ai:
                        report(int(done * 100 / len(pending_ai)))

        if pending_ai:
            for index, verdict in await asyncio.gather(
                *(review_one(i, p, b) for i, p, b in pending_ai)
            ):
                verdicts_by_index[index] = verdict

        return [
            {
                "match_result_id": result.id,
                "candidate_id": result.candidate_id,
                "jd_revision_id": result.jd_revision_id or run.jd_revision_id,
                **verdicts_by_index.get(
                    index,
                    {
                        "verdict": "pending",
                        "project_match": (), "experience_match": (), "tech_match": (), "risks": (),
                    },
                ),
            }
            for index, result in enumerate(results)
        ]


def _clean_lines(items) -> tuple[str, ...]:
    return tuple(text.strip() for text in (items or []) if text and text.strip())


def validated_verdict(model: ReviewVerdictModel) -> dict:
    """校验并清洗模型结论（匹配复核与搜索复核共用同一份 JSON 契约）。"""
    if model.verdict not in ("recommend", "pending", "reject"):
        raise ValueError(f"invalid verdict: {model.verdict}")
    return {
        "verdict": model.verdict,
        "project_match": _clean_lines(model.project_match),
        "experience_match": _clean_lines(model.experience_match),
        "tech_match": _clean_lines(model.tech_match),
        "risks": _clean_lines(model.risks),
    }
