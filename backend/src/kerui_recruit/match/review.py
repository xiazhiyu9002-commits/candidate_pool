"""可选 AI 深度复核：prompt 构造与结构化结论解析。

复核重点判断候选人过往经历（项目、业务、技术）与 JD 描述的项目、业务、技术是否匹配
（先项目与业务场景，再行业与技术栈），输出推荐理由与注意点（以点列出）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import select

from kerui_recruit.db.models import JdRevision, MatchResult, MatchRun, ResumeRevision
from kerui_recruit.providers.ai.contracts import ReasoningMode

# 并发复核上限，避免单批触发上游限流。
_REVIEW_CONCURRENCY = 5
# 单条复核的调用超时（秒）：一次挂死不能拖住整批；超时条目会显式标记出来。
_REVIEW_CALL_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    verdict: str  # "recommend" | "pending" | "reject"
    reasons: tuple[str, ...] = ()   # 推荐理由
    cautions: tuple[str, ...] = ()  # 注意点


class ReviewVerdictModel(BaseModel):
    verdict: str
    reasons: list[str] = []
    cautions: list[str] = []


def _failed_verdict(message: str, error: str) -> dict:
    """复核失败的条目：仍按「待核」呈现，但显式带上失败标记与原因。

    以前失败被静默写成 pending，用户看到的是「全是待核、没有结果」，无法区分
    「模型判不了」和「调用压根没成功」。
    """
    return {
        "verdict": "pending",
        "reasons": (),
        "cautions": (message,),
        "failed": True,
        "error": error,
    }


def build_review_prompt(jd_text: str, candidate_text: str) -> str:
    """构造复核 prompt：给岗位要求与候选人简述，输出推荐理由与注意点。

    重点对照岗位的业务/项目要求与候选人具体项目的业务场景、本人职责、技术与成果；
    每条结论附可追溯的证据编号，证据缺失时写「未见证据」，不得编造经历。
    """
    return (
        "你在复核一个岗位与候选人的匹配程度。请先比较岗位的核心业务/项目要求与候选人"
        "具体项目的业务场景、本人职责、技术栈与成果（项目与业务场景优先，其次行业与技术栈）。\n"
        f"岗位要求：\n{jd_text}\n"
        f"候选人简述（含项目/经历证据编号）：\n{candidate_text}\n"
        "只返回 JSON："
        '{"verdict":"recommend|pending|reject","reasons":["推荐理由…"],"cautions":["注意点…"]}。'
        "reasons 是推荐理由、cautions 是缺口或风险；每条结尾用括号标注对应证据编号"
        "（如 projects[0]、experiences[1]）；确无证据时写「未见证据」，不要臆造经历或技术。"
        "每条简短直接，不写过渡词、不写修饰语；没有则填空数组。"
    )


def build_candidate_text(data: dict) -> str:
    """候选人简述（含项目/经历证据编号），匹配复核与搜索复核共用同一口径。"""
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
        """复核这次 run 里**全部**合格配对：匹配到的每一个都要有结论。

        条数与整批时长都不设上限：一次 run 上千条也全跑完，靠 worker 每 30 秒续租保活，
        用户可随时「取消复核」。单条调用保留超时（一次挂死不能拖住整批），
        超时/失败的条目显式标记出来，不会退化成「没有结果」。
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
        pending_ai: list[tuple[int, str]] = []

        for index, result in enumerate(results):
            jd_revision = jd_revisions.get(result.jd_revision_id or run.jd_revision_id)
            jd_parsed = (jd_revision.parsed_data or {}) if jd_revision else {}
            data = candidate_data.get(result.candidate_id, {})
            # 资格被拒绝（方向不一致 / 必需技能缺失）的配对不进入 AI 复核。
            eligibility = evaluate_pair(jd_parsed, data).eligibility
            if eligibility == "rejected":
                verdicts_by_index[index] = {"verdict": "pending", "reasons": (), "cautions": ()}
                continue
            prompt = build_review_prompt(
                self._build_jd_text(jd_parsed),
                build_candidate_text(data),
            )
            pending_ai.append((index, prompt))

        semaphore = asyncio.Semaphore(_REVIEW_CONCURRENCY)
        done = 0

        async def review_one(index: int, prompt: str) -> tuple[int, dict]:
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
                    return index, validated_verdict(model)
                except asyncio.TimeoutError:
                    return index, _failed_verdict(
                        f"复核超时（单条超过 {int(_REVIEW_CALL_TIMEOUT_SECONDS)} 秒）", "TimeoutError"
                    )
                except Exception as error:  # noqa: BLE001 - 单对失败不阻断整批，但必须可见
                    return index, _failed_verdict(
                        f"复核失败：{type(error).__name__}", type(error).__name__
                    )
                finally:
                    done += 1
                    if report is not None and pending_ai:
                        report(int(done * 100 / len(pending_ai)))

        if pending_ai:
            for index, verdict in await asyncio.gather(*(review_one(i, p) for i, p in pending_ai)):
                verdicts_by_index[index] = verdict

        return [
            {
                "match_result_id": result.id,
                "candidate_id": result.candidate_id,
                "jd_revision_id": result.jd_revision_id or run.jd_revision_id,
                **verdicts_by_index.get(index, {"verdict": "pending", "reasons": (), "cautions": ()}),
            }
            for index, result in enumerate(results)
        ]

    @staticmethod
    def _build_jd_text(jd_parsed: dict) -> str:
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


def validated_verdict(model: ReviewVerdictModel) -> dict:
    """校验并清洗模型结论（匹配复核与搜索复核共用同一份 JSON 契约）。"""
    if model.verdict not in ("recommend", "pending", "reject"):
        raise ValueError(f"invalid verdict: {model.verdict}")
    reasons = tuple(r.strip() for r in (model.reasons or []) if r and r.strip())
    cautions = tuple(c.strip() for c in (model.cautions or []) if c and c.strip())
    return {"verdict": model.verdict, "reasons": reasons, "cautions": cautions}
