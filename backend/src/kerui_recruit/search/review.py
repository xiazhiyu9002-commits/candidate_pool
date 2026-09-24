"""搜索侧 AI 复核：按招聘方的**搜索条件**（而不是 JD）复核候选人，产出亮点与风险点。

结论独立落表 ``search_review``，按「查询指纹 + 候选人」唯一可查：同一个人在不同搜索
条件下的结论互不覆盖，同一条搜索条件重复复核则覆盖旧结论。

与匹配复核共用一份 JSON 契约（``ReviewVerdictModel``）与同一组提示词常量：三个维度
（项目 → 工作经历 → 技术栈）按顺序产出，落库时把三段按顺序拼进 ``highlights`` 列、
风险点进 ``risks`` 列。

输入只给**软性条件**（业务方向、技能、公司/职位等）与候选人技术、业务、项目信息：
学历、学校层次、年限、年龄、城市、QS、性别、姓名、排除技能等硬条件已在筛选阶段生效，
复核阶段不再评价（查询指纹仍按全量筛选条件计算，避免不同条件共用同一批结论）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import select

from kerui_recruit.db.models import ResumeDocument, ResumeRevision, SearchReview
from kerui_recruit.match.review import (
    REVIEW_OUTPUT_CONTRACT,
    REVIEW_VERDICT_RULES,
    REVIEW_WEIGHT_RULES,
    ReviewVerdictModel,
    apply_consistency_fallback,
    build_basis,
    build_candidate_text,
    validated_verdict,
)
from kerui_recruit.providers.ai.contracts import ReasoningMode

# 与匹配复核同一组取舍：并发受限、单条有超时、失败显式标记但不阻断整批。
_REVIEW_CONCURRENCY = 5
_REVIEW_CALL_TIMEOUT_SECONDS = 120.0

# 参与「查询指纹」与提示词的筛选字段（固定顺序，保证指纹稳定）。
_CONDITION_FIELDS = (
    "min_years", "max_years", "min_age", "max_age", "highest_degree",
    "location", "locations", "preferred_location", "preferred_locations",
    "max_qs_rank", "school_level", "exclude_skills", "phone", "gender", "name",
    "company", "title", "school", "direction", "school_region", "specializations",
    "career_directions", "career_specializations", "business_directions",
)

# 传给模型的软性条件：硬条件（学历/年限/年龄/城市/QS/学校层级/性别/姓名/排除技能）
# 已在筛选阶段生效，复核阶段再评价只会引入噪声；指纹仍用 _CONDITION_FIELDS 全量。
_SOFT_CONDITION_FIELDS = (
    "company", "title", "direction", "specializations",
    "career_directions", "career_specializations", "business_directions",
)


_CONDITION_LABELS = {
    "min_years": "最低工作年限",
    "max_years": "最高工作年限",
    "min_age": "最低年龄",
    "max_age": "最高年龄",
    "highest_degree": "最低学历",
    "location": "现居城市",
    "locations": "现居城市",
    "preferred_location": "意向城市",
    "preferred_locations": "意向城市",
    "max_qs_rank": "QS 最高排名",
    "school_level": "学校等级",
    "exclude_skills": "排除技能",
    "phone": "手机号",
    "gender": "性别",
    "name": "姓名",
    "company": "公司",
    "title": "职位",
    "school": "学校",
    "direction": "职业方向",
    "school_region": "国内外高校",
    "specializations": "专业",
    "career_directions": "职业方向",
    "career_specializations": "职业细分",
    "business_directions": "业务方向",
}


def _normalize_query(query: str) -> str:
    return (query or "").strip()


def _normalized_conditions(filters: dict | None) -> dict:
    """只保留有值的筛选字段；空值/空列表不进入指纹，避免「空筛选」与「未传筛选」分裂。"""
    source = filters or {}
    result: dict = {}
    for field in _CONDITION_FIELDS:
        value = source.get(field)
        if value is None or value == "" or value == () or value == []:
            continue
        if isinstance(value, (list, tuple)):
            items = [str(item) for item in value if item not in (None, "")]
            if not items:
                continue
            result[field] = items
        else:
            result[field] = str(value)
    return result


def query_fingerprint(query: str, filters: dict | None = None) -> str:
    """搜索条件的稳定指纹：同一条件重复复核复用同一批结论。"""
    payload = json.dumps(
        {"query": _normalize_query(query), "filters": _normalized_conditions(filters)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def review_base_key(query_key: str, candidate_ids: Sequence[str]) -> str:
    """复核任务的幂等键基座：同一搜索条件 + 同一批候选人才算同一次复核。"""
    digest = hashlib.sha256(",".join(sorted(set(candidate_ids))).encode("utf-8")).hexdigest()[:12]
    return f"SEARCH_REVIEW:{query_key}:{digest}"


def describe_conditions(query: str, filters: dict | None = None) -> str:
    """把搜索条件写成给模型看的一段话：只给软性条件——硬条件已在筛选阶段生效。"""
    lines: list[str] = []
    text = _normalize_query(query)
    if text:
        lines.append(f"关键词：{text}")
    conditions = _normalized_conditions(filters)
    soft = {field: conditions[field] for field in _SOFT_CONDITION_FIELDS if field in conditions}
    if soft:
        rendered = "；".join(
            f"{_CONDITION_LABELS.get(field, field)}："
            + ("、".join(value) if isinstance(value, list) else value)
            for field, value in soft.items()
        )
        lines.append(f"软性要求：{rendered}")
    return "\n".join(lines) or "（招聘方没有给出明确条件，属于泛检索）"


def build_query_review_prompt(conditions: str, candidate_text: str) -> str:
    """搜索复核 prompt：与匹配复核共用权重、三档判据与 JSON 契约，只输出三个维度的匹配点与风险点。"""
    return (
        "你在按招聘方的搜索条件复核一位候选人。只看技术、业务与项目层面："
        "学历、学校层次、年限、城市等硬条件已在前面筛过，不要评价也不要复述。\n"
        f"{REVIEW_WEIGHT_RULES}\n"
        f"搜索条件：\n{conditions}\n"
        f"候选人（含项目/经历证据编号）：\n{candidate_text}\n"
        "按下面三点给结论，顺序固定（没有内容的维度填空数组）：\n"
        "1. 项目：候选人的项目经历与搜索条件里的业务、技术要求是否匹配；\n"
        "2. 工作经历：候选人的工作经历与搜索条件里的业务、技术要求是否匹配；\n"
        "3. 技术栈：候选人的技术栈与搜索条件里的技术要求是否匹配。\n"
        f"{REVIEW_VERDICT_RULES}\n"
        f"{REVIEW_OUTPUT_CONTRACT}"
    )


def _failed_verdict(message: str, error: str) -> dict:
    """复核失败的条目：仍按「待核」呈现，但显式带上失败标记与原因。"""
    return {
        "verdict": "pending",
        "project_match": (),
        "experience_match": (),
        "tech_match": (),
        "risks": (message,),
        "failed": True,
        "error": error,
    }


def _highlights(verdict: dict) -> list[str]:
    """把三个维度按「项目 → 工作经历 → 技术栈」顺序拼成亮点列。"""
    merged: list[str] = []
    for key in ("project_match", "experience_match", "tech_match"):
        merged.extend(verdict.get(key) or ())
    return merged


class SearchReviewService:
    """对「一次搜索条件 + 一批候选人」做 AI 复核，结论落表可查。"""

    def __init__(self, session_factory, task_client, reasoning_task_client=None) -> None:
        self.session_factory = session_factory
        self.task_client = task_client
        self.reasoning_task_client = reasoning_task_client

    async def review_query(
        self,
        *,
        query_key: str,
        conditions: str,
        candidate_ids: Sequence[str],
        report=None,
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> dict:
        """复核给定候选人：条数与整批时长都不设上限，靠 worker 续租保活、可随时取消。

        单条调用保留超时；失败/超时条目显式带标记落库，不会退化成「没有结论」。
        """
        client = (
            self.reasoning_task_client
            if reasoning == ReasoningMode.REQUIRED and self.reasoning_task_client is not None
            else self.task_client
        )
        ordered = list(dict.fromkeys(candidate_ids))  # 去重且保持传入顺序
        if not ordered:
            return {"query_key": query_key, "reviewed": 0, "failed": 0}
        with self.session_factory() as session:
            rows = session.execute(
                select(ResumeDocument.candidate_id, ResumeRevision.id, ResumeRevision.parsed_data)
                .join(ResumeRevision, ResumeRevision.document_id == ResumeDocument.id)
                .where(ResumeDocument.candidate_id.in_(ordered),
                       ResumeRevision.is_current.is_(True),
                       ResumeRevision.status == "READY")
            ).all()
        revisions = {candidate_id: revision_id for candidate_id, revision_id, _ in rows}
        parsed = {candidate_id: (data or {}) for candidate_id, _, data in rows}

        total = len(ordered)
        done = 0
        failed = 0
        # 没有可用简历版本的候选人：直接记为待核，不能假装复核过。
        for candidate_id in ordered:
            if candidate_id in parsed:
                continue
            self._upsert(query_key, conditions, candidate_id, None,
                         {**_failed_verdict("简历尚未解析完成，无法复核", "NO_PARSED_RESUME"),
                          "basis": build_basis(source="search", eligible=True)})
            done += 1
            failed += 1
            if report is not None:
                report(int(done * 100 / total))

        # 搜索侧没有前置资格判定：硬条件已在筛选阶段生效，这里只判断软性条件的匹配。
        pending_ai = [
            (
                candidate_id,
                build_query_review_prompt(conditions, build_candidate_text(parsed[candidate_id])),
                build_basis(source="search", eligible=True),
            )
            for candidate_id in ordered
            if candidate_id in parsed
        ]

        semaphore = asyncio.Semaphore(_REVIEW_CONCURRENCY)

        async def review_one(candidate_id: str, prompt: str, basis: dict) -> tuple[str, dict]:
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
                    return candidate_id, {**cleaned, "basis": final_basis}
                except asyncio.TimeoutError:
                    return candidate_id, {
                        **_failed_verdict(
                            f"复核超时（单条超过 {int(_REVIEW_CALL_TIMEOUT_SECONDS)} 秒）", "TimeoutError"
                        ),
                        "basis": basis,
                    }
                except Exception as error:  # noqa: BLE001 - 单条失败不阻断整批，但必须可见
                    return candidate_id, {
                        **_failed_verdict(f"复核失败：{type(error).__name__}", type(error).__name__),
                        "basis": basis,
                    }

        tasks = [
            asyncio.create_task(review_one(candidate_id, prompt, basis))
            for candidate_id, prompt, basis in pending_ai
        ]
        try:
            # 逐条完成即落库：中途取消时已出结论的部分依然可查。
            for completed in asyncio.as_completed(tasks):
                candidate_id, verdict = await completed
                self._upsert(query_key, conditions, candidate_id,
                             revisions.get(candidate_id), verdict)
                done += 1
                if verdict.get("failed"):
                    failed += 1
                if report is not None:
                    report(int(done * 100 / total))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {"query_key": query_key, "reviewed": done, "failed": failed}

    def _upsert(self, query_key: str, conditions: str, candidate_id: str,
                revision_id: str | None, verdict: dict) -> None:
        """同一「查询 + 候选人」只保留一条结论：重复复核覆盖旧值。"""
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(SearchReview).where(
                    SearchReview.query_key == query_key,
                    SearchReview.candidate_id == candidate_id,
                )
            )
            if row is None:
                row = SearchReview(query_key=query_key, candidate_id=candidate_id)
                session.add(row)
            row.conditions_text = conditions
            row.revision_id = revision_id
            row.verdict = verdict["verdict"]
            row.highlights = _highlights(verdict)
            row.risks = list(verdict.get("risks") or ())
            row.basis = verdict.get("basis")
            row.failed = bool(verdict.get("failed", False))
            row.error = verdict.get("error")
