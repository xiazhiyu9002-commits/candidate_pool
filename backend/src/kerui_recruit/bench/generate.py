from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkCandidate:
    candidate_id: str
    revision_id: str
    chunks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkQuery:
    text: str
    relevant_candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    candidates: tuple[BenchmarkCandidate, ...]
    queries: tuple[BenchmarkQuery, ...]


def generate_dataset(candidate_count: int, seed: int) -> BenchmarkDataset:
    """Deterministically build candidates and labeled queries for the gate.

    Each query owns a unique tag that only its relevant candidates carry, so
    recall can be measured without a live embedding model. The tag is a long
    random token to avoid n-gram ambiguity with the other tags.
    """
    rng = random.Random(seed)
    query_count = max(1, candidate_count // 60)
    candidates: list[BenchmarkCandidate] = []
    queries: list[BenchmarkQuery] = []

    # Assign three relevant candidates to each query.
    for query_index in range(query_count):
        tag = f"tag{query_index:05d}x{rng.getrandbits(64):016x}"
        relevant: list[str] = []
        for offset in range(3):
            candidate_index = query_index * 3 + offset
            if candidate_index >= candidate_count:
                break
            candidate_id = f"cand-{candidate_index:08d}"
            revision_id = f"rev-{candidate_index:08d}"
            relevant.append(candidate_id)
            candidates.append(
                BenchmarkCandidate(
                    candidate_id=candidate_id,
                    revision_id=revision_id,
                    chunks=(
                        f"{tag} 高级工程师 {rng.choice(('金融风控', '支付结算', '电商平台'))}",
                        f"{tag} 负责核心系统 {rng.choice(('Java', 'Python', 'Go'))} 服务",
                        f"{tag} 项目经验 {rng.choice(('架构设计', '性能优化', '稳定性治理'))}",
                    ),
                )
            )
        if relevant:
            queries.append(
                BenchmarkQuery(text=tag, relevant_candidate_ids=tuple(relevant))
            )

    # Fill the rest with distractors that carry no query tag.
    for candidate_index in range(query_count * 3, candidate_count):
        rng.getrandbits(64)
        candidates.append(
            BenchmarkCandidate(
                candidate_id=f"cand-{candidate_index:08d}",
                revision_id=f"rev-{candidate_index:08d}",
                chunks=(
                    f"背景噪音 {rng.choice(('平面设计', '市场运营', '行政助理'))} {rng.getrandbits(64):016x}",
                    f"普通岗位 {rng.choice(('文案', '客服', '资料整理'))} {rng.getrandbits(64):016x}",
                    f"其他方向 {rng.choice(('自媒体', '销售', '仓储'))} {rng.getrandbits(64):016x}",
                ),
            )
        )

    return BenchmarkDataset(candidates=tuple(candidates), queries=tuple(queries))