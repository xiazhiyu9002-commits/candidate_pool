"""JD 候选人画像输入构建、哈希与生成策略。

提示词骨架、硬门槛口径、量化口径与增量修正规则统一来自 ``providers.profile_spec``，
与候选人 AI 画像共用同一套规范。

独立生成链路只让模型产**要点 + 硬条件**：整段、facts、compact 由要点本地派生
（``pair_from_points``）。两种形态本来就要求逐字同源，让模型写两遍只会把思考量放大到
数倍（实测单条 7k 思考 token 只为产出百来字的正文），一致性还常被本地重建覆盖。
解析期内联那条链路（快模型、画像只是解析产物中的一个字段）不受影响，仍按整段口径产出。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from pydantic import BaseModel, Field

from kerui_recruit.jd.profile_constraints import (
    CONSTRAINT_FIELD_SPEC,
    CONSTRAINT_PARSE_PROMPT,
    YEAR_FIELD_SPEC,
    ProfileRequirements,
    interpret_model_years,
    normalize_constraints,
)
from kerui_recruit.jd.structured import ConstraintList, ExactConstraint
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.profile_pair import ProfilePair, ProfilePoint, pair_from_points

logger = logging.getLogger(__name__)


class JdProfilePair(ProfilePair):
    """JD 画像双形态 + 该 JD 的硬条件草稿（与画像同一次调用产出，零额外调用）。"""

    exact_constraints: list[ExactConstraint] = Field(default_factory=list)


class JdPointsPlan(BaseModel):
    """JD 画像的模型产物：要点 + 硬条件；整段 / facts / compact 由要点本地派生。"""

    points: list[ProfilePoint] = Field(default_factory=list)
    exact_constraints: list[ExactConstraint] = Field(default_factory=list)


class JdProfileGenerator:
    _FIELDS = profile_spec.JD_PROFILE_INPUT_FIELDS

    def __init__(self, llm) -> None:
        self._llm = llm

    def build_input(self, data: dict) -> dict:
        return {key: data.get(key) for key in self._FIELDS if data.get(key) not in (None, "", [])}

    def input_hash(self, data: dict) -> str:
        payload = json.dumps(self.build_input(data), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _render(self, data: dict) -> str:
        return json.dumps(self.build_input(data), ensure_ascii=False, default=str)

    async def generate(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None) -> str:
        prompt = profile_spec.JD_PROFILE_TEMPLATE.format(
            instruction=profile_spec.instruction_block(instruction),
            previous=profile_spec.previous_block(previous),
            evidence=self._render(data),
        )
        text = await self._llm.complete_text(
            [{"role": "user", "content": prompt}],
            **({"execution_context": execution_context} if execution_context is not None else {}),
        )
        return text.strip()

    async def generate_pair(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None, on_stage=None) -> JdProfilePair:
        """一次结构化生成 narrative + points + compact + 硬条件，并做一次校验驱动的定点重写。"""
        from kerui_recruit.providers.profile_pair import produce_pair_with_vet

        async def produce(rewrite_instruction: str | None, _rewrite_previous: str | None):
            if rewrite_instruction is not None:
                return await self._repair(rewrite_instruction, execution_context=execution_context)
            return await self._produce_pair(
                data, instruction=instruction, previous=previous, execution_context=execution_context)

        pair = await produce_pair_with_vet(produce, side="jd", logger=logger, on_stage=on_stage)
        return self._replace_generic_mainline(pair, data)

    # 「技术主线为分布式系统、微服务架构…」这类通用要求当技术栈的写法，模型即使被点名也照抄 JD 原文。
    # 措辞不固定：技术主线 / 技术栈 / 技术要求 / 技术方向都要能兜住。
    _GENERIC_MAINLINE = re.compile(r"(技术主线|技术栈|技术要求|技术方向)[^。]*。")
    # 通用要求与 OR 并列词：不能拿来当技术栈（猎头看不懂、没有区分度）。
    _GENERIC_SKILL_WORDS = (
        "分布式", "微服务", "高并发", "服务治理", "性能调优", "架构", "系统设计",
        "分库分表", "分布式事务", "限流", "熔断", "消息队列", "缓存", "对象存储", "数据库",
        " or ", "或", "/", "、",
    )

    def _replace_generic_mainline(self, pair: JdProfilePair, data: dict) -> JdProfilePair:
        """确定性兜底：技术主线仍是通用要求时，把那条要点换成该 JD 的必备技能。

        模型对 JD 原文有强照抄倾向，提示词与定点重写都改不动，所以这里不再依赖模型：
        从 required_skills / must_skill_groups 里挑出**具体技术名词**重建主线句，其余要点原样保留。
        挑不出可用名词时**删掉那条要点**，而不是留着它或编一个名字；
        替换/删除后若掉出字数区间或出现别的问题，就保持原样（宁可留一句通用要求，也不能让画像过短）。
        """
        if not any("通用要求" in issue for issue in profile_spec.vet_profile(pair.narrative, side="jd")):
            return pair
        skills = self._concrete_skills(data)
        replacement = ("技术栈为" + "、".join(skills) + "。") if skills else ""
        points: list[ProfilePoint] = []
        hit = False
        for point in pair.points:
            if hit or not self._GENERIC_MAINLINE.search(point.text):
                points.append(point)
                continue
            hit = True
            if replacement:
                points.append(ProfilePoint(
                    text=self._GENERIC_MAINLINE.sub(replacement, point.text, count=1).strip(),
                    evidence_paths=list(point.evidence_paths),
                ))
            # 挑不出具体技术名词：整条丢掉，不再保留那句通用要求
        if not hit:
            return pair
        fixed = pair_from_points(points)
        if not replacement and profile_spec.vet_profile(fixed.narrative, side="jd"):
            return pair  # 删完掉出字数区间或有别的问题：宁可留一句通用要求，也不能让画像过短
        logger.info("技术主线仍是通用要求，已按 JD 必备技能确定性替换或删除：skills=%s", skills)
        return JdProfilePair(**fixed.model_dump(), exact_constraints=pair.exact_constraints)

    def _concrete_skills(self, data: dict) -> list[str]:
        """从 JD 结构化字段里挑具体技术名词（最多 2 个），供技术主线兜底替换。"""
        candidates = [str(skill).strip() for skill in (data.get("required_skills") or []) if str(skill).strip()]
        for group in data.get("must_skill_groups") or []:
            if isinstance(group, dict):
                candidates += [str(item).strip() for item in (group.get("alternatives") or []) if str(item).strip()]
        usable = [
            skill for skill in candidates
            if not any(word in skill for word in self._GENERIC_SKILL_WORDS) and len(skill) <= 20
        ]
        return list(dict.fromkeys(usable))[:2]

    async def _repair(self, instruction: str, *, execution_context=None) -> JdProfilePair:
        """定点修正：只把「草稿 + 违规原因 + 修正要求」发给模型，再按句读拆点。

        定点修正没有硬条件产出，返回空数组由调用方保留既有约束，避免静默清空。
        """
        from kerui_recruit.providers.profile_pair import build_profile_pair
        text = await self._llm.complete_text(
            [{"role": "user", "content": instruction}],
            **({"execution_context": execution_context} if execution_context is not None else {}),
            max_tokens=profile_spec.PROFILE_REWRITE_MAX_TOKENS,
        )
        return JdProfilePair(**build_profile_pair(text).model_dump())

    async def _produce_pair(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None) -> JdProfilePair:
        """产出一次 JD 画像：模型只给要点与硬条件，整段/facts/compact 由要点本地派生。

        结构化 JSON 不可用或要点为空时退化为单文本（按句读拆点），保证链路始终有产出。
        """
        from kerui_recruit.providers.profile_pair import build_profile_pair
        if hasattr(self._llm, "complete_json"):
            prompt = profile_spec.JD_POINTS_TEMPLATE.format(
                instruction=profile_spec.instruction_block(instruction),
                previous=profile_spec.previous_block(previous),
                evidence=self._render(data),
            )
            try:
                plan = await self._llm.complete_json(
                    [{"role": "user", "content": prompt}],
                    JdPointsPlan,
                    **({"execution_context": execution_context} if execution_context is not None else {}),
                )
                pair = pair_from_points(getattr(plan, "points", None) or ())
                if pair.narrative.strip():
                    return JdProfilePair(
                        **pair.model_dump(),
                        exact_constraints=normalize_constraints(
                            getattr(plan, "exact_constraints", None)
                        ),
                    )
                logger.warning(
                    "JD 画像要点为空，退化为单文本拆点：llm=%s", type(self._llm).__name__,
                )
            except Exception as error:  # noqa: BLE001 - 退化路径必须留痕，不能静默
                logger.warning(
                    "JD 画像结构化生成失败，退化为单文本拆点：llm=%s error=%s code=%s detail=%s",
                    type(self._llm).__name__, type(error).__name__,
                    getattr(error, "code", None), error,
                )
        else:
            logger.warning(
                "JD 画像生成器未提供 complete_json，无法产出要点：llm=%s",
                type(self._llm).__name__,
            )
        narrative = await self.generate(data, instruction=instruction, previous=previous, execution_context=execution_context)
        # 退化路径没有硬条件，返回空数组由调用方保留既有约束，避免静默清空。
        return JdProfilePair(**build_profile_pair(narrative).model_dump())

    async def parse_constraints(self, source_text: str, *, execution_context=None) -> ProfileRequirements:
        """按给定文本重解析硬条件与年限（AI 判定；模型不可用时返回空结论，由调用方回退规则）。

        人工改过画像文本后必须重算，否则要求与画像对不上。硬条件与年限**同一次调用**产出，
        不额外增加模型调用次数。
        """
        if not source_text.strip() or not hasattr(self._llm, "complete_json"):
            return ProfileRequirements(constraints=[], min_years=None, years_stated=False)
        prompt = CONSTRAINT_PARSE_PROMPT.format(
            spec=CONSTRAINT_FIELD_SPEC, year_spec=YEAR_FIELD_SPEC, source_text=source_text
        )
        result = await self._llm.complete_json(
            [{"role": "user", "content": prompt}],
            ConstraintList,
            **({"execution_context": execution_context} if execution_context is not None else {}),
        )
        stated, years = interpret_model_years(getattr(result, "min_years", None))
        return ProfileRequirements(
            constraints=normalize_constraints(getattr(result, "constraints", None)),
            min_years=years,
            years_stated=stated,
        )
