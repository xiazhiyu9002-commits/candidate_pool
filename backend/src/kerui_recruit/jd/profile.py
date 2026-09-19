"""JD 候选人画像输入构建、哈希与生成策略。

提示词骨架、硬门槛口径、量化口径与增量修正规则统一来自 ``providers.profile_spec``，
与候选人 AI 画像共用同一套规范。
"""
from __future__ import annotations

import hashlib
import json
import logging

from pydantic import Field

from kerui_recruit.jd.profile_constraints import (
    CONSTRAINT_FIELD_SPEC,
    CONSTRAINT_PARSE_PROMPT,
    normalize_constraints,
)
from kerui_recruit.jd.structured import ConstraintList, ExactConstraint
from kerui_recruit.providers import profile_spec
from kerui_recruit.providers.profile_pair import ProfilePair

logger = logging.getLogger(__name__)


class JdProfilePair(ProfilePair):
    """JD 画像双形态 + 该 JD 的硬条件草稿（与画像同一次调用产出，零额外调用）。"""

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

    async def generate_pair(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None) -> JdProfilePair:
        """一次结构化生成 narrative + points + compact + 硬条件；失败回退单文本拆点。"""
        from kerui_recruit.providers.profile_pair import build_profile_pair, reconcile_pair
        if hasattr(self._llm, "complete_json"):
            prompt = profile_spec.JD_PAIR_TEMPLATE.format(
                instruction=profile_spec.instruction_block(instruction),
                previous=profile_spec.previous_block(previous),
                evidence=self._render(data),
            )
            try:
                pair = await self._llm.complete_json(
                    [{"role": "user", "content": prompt}],
                    JdProfilePair,
                    **({"execution_context": execution_context} if execution_context is not None else {}),
                )
                if isinstance(pair, ProfilePair) and pair.narrative.strip():
                    reconciled = reconcile_pair(pair)
                    if reconciled is not pair:
                        logger.warning(
                            "JD 画像双形态不同源（facts 未逐字命中整体与分点），已按分点重建：llm=%s facts=%d points=%d",
                            type(self._llm).__name__, len(pair.key_facts()), len(pair.points),
                        )
                    return JdProfilePair(
                        facts=reconciled.facts,
                        narrative=reconciled.narrative,
                        points=reconciled.points,
                        compact=reconciled.compact,
                        exact_constraints=normalize_constraints(
                            getattr(pair, "exact_constraints", None)
                        ),
                    )
                logger.warning(
                    "JD 画像双形态生成未返回可用 narrative，退化为单文本拆点：llm=%s",
                    type(self._llm).__name__,
                )
            except Exception as error:  # noqa: BLE001 - 退化路径必须留痕，不能静默
                logger.warning(
                    "JD 画像双形态生成失败，退化为单文本拆点：llm=%s error=%s code=%s detail=%s",
                    type(self._llm).__name__, type(error).__name__,
                    getattr(error, "code", None), error,
                )
        else:
            logger.warning(
                "JD 画像生成器未提供 complete_json，无法产出真双形态：llm=%s",
                type(self._llm).__name__,
            )
        narrative = await self.generate(data, instruction=instruction, previous=previous, execution_context=execution_context)
        # 退化路径没有硬条件，返回空数组由调用方保留既有约束，避免静默清空。
        return JdProfilePair(**build_profile_pair(narrative).model_dump())

    async def parse_constraints(self, source_text: str, *, execution_context=None) -> list[dict]:
        """按给定文本重解析硬条件（AI 判定 MUST/PLUS/EXCLUDE）。

        人工改过画像文本后必须重算，否则硬条件与画像对不上；模型不可用时返回空列表，
        由调用方决定是否回退到确定性规则。
        """
        if not source_text.strip() or not hasattr(self._llm, "complete_json"):
            return []
        prompt = CONSTRAINT_PARSE_PROMPT.format(spec=CONSTRAINT_FIELD_SPEC, source_text=source_text)
        result = await self._llm.complete_json(
            [{"role": "user", "content": prompt}],
            ConstraintList,
            **({"execution_context": execution_context} if execution_context is not None else {}),
        )
        return normalize_constraints(getattr(result, "constraints", None))
