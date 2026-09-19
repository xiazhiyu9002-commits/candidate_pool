"""候选人 AI 画像输入构建、哈希与生成策略。

画像只能基于结构化简历证据生成，不读取原始简历正文，避免把原始文本与
结构化字段混在一起。生成失败不阻断回填，调用方负责置空并记录诊断。
提示词骨架、输出形态与字数口径统一来自 ``providers.profile_spec``。
"""
from __future__ import annotations

import hashlib
import json
import logging

from kerui_recruit.providers import profile_spec

logger = logging.getLogger(__name__)

# 参与画像生成的输入字段，用于计算稳定的输入哈希。
PROFILE_INPUT_FIELDS = profile_spec.CANDIDATE_PROFILE_INPUT_FIELDS


def profile_input_hash(data: dict) -> str:
    """根据画像输入字段计算稳定哈希，用于判断画像是否已过期。"""
    payload = json.dumps(
        {key: data.get(key) for key in PROFILE_INPUT_FIELDS if data.get(key) not in (None, "", [])},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CandidateProfileGenerator:
    """从结构化简历字段生成/更新候选人画像，并跟踪输入哈希。"""

    _FIELDS = PROFILE_INPUT_FIELDS

    def __init__(self, llm) -> None:
        # llm 需要提供 ``async def complete_text(messages) -> str``。
        self._llm = llm

    def build_input(self, data: dict) -> dict:
        return {key: data.get(key) for key in self._FIELDS if data.get(key) not in (None, "", [])}

    def input_hash(self, data: dict) -> str:
        return profile_input_hash(data)

    def _render(self, data: dict) -> str:
        return json.dumps(self.build_input(data), ensure_ascii=False, default=str)

    async def generate(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None) -> str:
        prompt = profile_spec.CANDIDATE_PROFILE_TEMPLATE.format(
            instruction=profile_spec.instruction_block(instruction),
            previous=profile_spec.previous_block(previous),
            evidence=self._render(data),
        )
        text = await self._llm.complete_text(
            [{"role": "user", "content": prompt}],
            **({"execution_context": execution_context} if execution_context is not None else {}),
        )
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    async def generate_pair(self, data: dict, instruction: str | None = None, previous: str | None = None, *, execution_context=None):
        """一次结构化生成 facts + narrative + points + compact（同源）；失败回退单文本拆点。"""
        from kerui_recruit.providers.profile_pair import ProfilePair, build_profile_pair, reconcile_pair
        if hasattr(self._llm, "complete_json"):
            prompt = profile_spec.CANDIDATE_PAIR_TEMPLATE.format(
                instruction=profile_spec.instruction_block(instruction),
                previous=profile_spec.previous_block(previous),
                evidence=self._render(data),
            )
            try:
                pair = await self._llm.complete_json(
                    [{"role": "user", "content": prompt}],
                    ProfilePair,
                    **({"execution_context": execution_context} if execution_context is not None else {}),
                )
                if isinstance(pair, ProfilePair) and pair.narrative.strip():
                    reconciled = reconcile_pair(pair)
                    if reconciled is not pair:
                        logger.warning(
                            "候选人画像双形态不同源（facts 未逐字命中整体与分点），已按分点重建：llm=%s facts=%d points=%d",
                            type(self._llm).__name__, len(pair.key_facts()), len(pair.points),
                        )
                    return reconciled
                logger.warning(
                    "候选人画像双形态生成未返回可用 narrative，退化为单文本拆点：llm=%s",
                    type(self._llm).__name__,
                )
            except Exception as error:  # noqa: BLE001 - 退化路径必须留痕，不能静默
                logger.warning(
                    "候选人画像双形态生成失败，退化为单文本拆点：llm=%s error=%s code=%s detail=%s",
                    type(self._llm).__name__, type(error).__name__,
                    getattr(error, "code", None), error,
                )
        else:
            logger.warning(
                "候选人画像生成器未提供 complete_json，无法产出真双形态：llm=%s",
                type(self._llm).__name__,
            )
        narrative = await self.generate(data, instruction=instruction, previous=previous, execution_context=execution_context)
        return build_profile_pair(narrative)
