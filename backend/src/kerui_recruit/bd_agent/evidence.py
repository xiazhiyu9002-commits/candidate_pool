from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from kerui_recruit.providers.contracts import RerankerProvider


@dataclass(frozen=True, slots=True)
class EvidenceDoc:
    source_url: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class RankedChunk:
    text: str
    source_url: str
    score: float | None = None


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？.!?])")

# 招聘平台 / 高质量招聘信息源
_RECRUITMENT_SOURCE_KEYWORDS = (
    "bosszhipin.com", "zhipin.com",
    "maimai.cn",
    "liepin.com",
    "linkedin.com",
    "51job.com",
    "lagou.com",
    "zhaopin.com",
    "kanzhun.com",
    "jobui.com",
)

# 低质量来源：新闻、媒体、博客、门户，通常不含单一在招岗位信息
_LOW_QUALITY_SOURCE_KEYWORDS = (
    "zhihu.com", "sohu.com", "sina.com", "163.com", "qq.com", "ifeng.com",
    "thepaper.cn", "36kr.com", "leiphone.com", "ithome.com", "csdn.net",
    "juejin.cn", "cnblogs.com", "segmentfault.com", "medium.com", "gov.cn",
    "weixin.qq.com", "baijiahao.baidu.com", "mp.weixin",
)

# 公司官网招聘路径标记
_CAREERS_PATH_MARKERS = (
    "careers", "career", "/jobs", "job/", "join-us", "joinus", "recruit",
    "zhaopin", "talent", "hr.",
)


def source_quality(url: str) -> float:
    """Estimate how useful a source URL is for BD lead discovery (0~1)."""
    host = (urlparse(url).netloc or "").lower()
    lowered = url.lower()
    if not host:
        return 0.0
    if any(keyword in host for keyword in _LOW_QUALITY_SOURCE_KEYWORDS):
        return 0.0
    if any(keyword in host for keyword in _RECRUITMENT_SOURCE_KEYWORDS):
        return 1.0
    if any(marker in lowered for marker in _CAREERS_PATH_MARKERS):
        return 0.8
    return 0.5


def _chunk_text(text: str, max_len: int = 600) -> list[str]:
    """Split text into sentence-aligned chunks of roughly max_len chars."""
    text = (text or "").strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_len:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current}{sentence}" if current else sentence
    if current:
        chunks.append(current)
    return chunks


class EvidenceExtractor:
    """Split fetched pages into chunks and rank them by source quality and
    relevance to the query.

    Low-value sources (news, blogs, portals) are dropped, then recruitment
    platforms and company career pages are preferred over neutral domains. The
    injected reranker orders chunks within the same quality tier.
    """

    def __init__(
        self,
        reranker: RerankerProvider | None = None,
        top_k: int = 10,
    ) -> None:
        self._reranker = reranker
        self.top_k = top_k

    async def extract(
        self,
        query: str,
        docs: list[EvidenceDoc],
    ) -> list[RankedChunk]:
        useful = [d for d in docs if source_quality(d.source_url) > 0]
        candidates: list[tuple[str, str]] = []
        for doc in useful:
            for chunk in _chunk_text(doc.content):
                candidates.append((chunk, doc.source_url))

        if not candidates:
            return []

        texts = [text for text, _ in candidates]
        if self._reranker is not None:
            try:
                order = await self._reranker.rerank(query, texts)
            except Exception:
                order = list(range(len(texts)))
        else:
            order = list(range(len(texts)))

        rank = {index: position for position, index in enumerate(order)}
        sorted_indices = sorted(
            range(len(candidates)),
            key=lambda i: (
                -source_quality(candidates[i][1]),
                rank.get(i, len(candidates)),
            ),
        )

        chunks: list[RankedChunk] = []
        for index in sorted_indices[: self.top_k]:
            text, source_url = candidates[index]
            chunks.append(
                RankedChunk(
                    text=text,
                    source_url=source_url,
                    score=source_quality(source_url),
                )
            )
        return chunks
