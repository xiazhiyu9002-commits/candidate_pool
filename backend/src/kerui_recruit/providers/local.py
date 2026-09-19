from __future__ import annotations

import hashlib
import math
import re

from kerui_recruit.jd.extract import split_jd_text
from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.resumes.structured import ParsedResume


_EXPERIENCE_PATTERNS = (
    re.compile(
        r"(?P<years>(?<!\d)\d{1,2}(?:\.\d+)?)(?!\d)\s*年\s*"
        r"(?:及?\s*以上\s*)?(?:工作|从业|相关)?\s*(?:经验|经历)",
        re.I,
    ),
    re.compile(
        r"(?:工作|从业|相关)\s*(?:经验|经历|年限)\s*[:：]?\s*"
        r"(?P<years>(?<!\d)\d{1,2}(?:\.\d+)?)(?!\d)\s*年",
        re.I,
    ),
    re.compile(
        r"(?P<years>(?<!\d)\d{1,2}(?:\.\d+)?)(?!\d)\s*"
        r"years?\s+(?:of\s+)?experience",
        re.I,
    ),
)


def _extract_experience_years(text: str) -> float | None:
    for pattern in _EXPERIENCE_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return float(match.group("years"))
    return None


class LocalHashEmbeddingProvider:
    """Deterministic packaged fallback used before a model API is configured."""

    def __init__(self, *, dimension: int = 64) -> None:
        self.dimension = dimension

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
            values.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        vector = values[: self.dimension]
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]


class LocalKeywordReranker:
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        terms = tuple(term.casefold() for term in query.split() if term)
        scores = [
            (sum(document.casefold().count(term) for term in terms), index)
            for index, document in enumerate(documents)
        ]
        return [index for _, index in sorted(scores, key=lambda item: (-item[0], item[1]))]


class LocalResumeParser:
    """Conservative offline parser; unknown fields remain unknown."""

    skill_terms = (
        "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++",
        "React", "Vue", "SQL", "金融", "风控", "支付", "招聘", "销售"
    )
    locations = ("北京", "上海", "深圳", "广州", "杭州", "成都", "Hong Kong")
    industries = ("互联网", "金融", "制造", "零售", "教育", "医疗", "房地产", "汽车")

    async def parse_resume(self, text: str) -> ParsedResume:
        compact = " ".join(text.split())
        total_years = _extract_experience_years(compact)
        degree = None
        for token, normalized in (
            ("博士", "DOCTORATE"), ("PhD", "DOCTORATE"),
            ("硕士", "MASTER"), ("Master", "MASTER"),
            ("本科", "BACHELOR"), ("Bachelor", "BACHELOR"),
            ("大专", "ASSOCIATE"),
        ):
            if token.casefold() in compact.casefold():
                degree = normalized
                break
        location = next(
            (item for item in self.locations if item.casefold() in compact.casefold()),
            None,
        )
        industry = next(
            (item for item in self.industries if item.casefold() in compact.casefold()),
            None,
        )
        skills = [
            item for item in self.skill_terms if item.casefold() in compact.casefold()
        ]
        gender = None
        if "女" in compact:
            gender = "女"
        elif "男" in compact:
            gender = "男"
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "待识别")
        return ParsedResume(
            name=first_line[:80],
            total_years=total_years,
            highest_degree=degree,
            location=location,
            gender=gender,
            industry=industry,
            skills=skills,
            summary=compact[:1_000],
        )


class LocalJdParser:
    """Conservative offline JD parser; hard requirements stay minimal."""

    skill_terms = (
        "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++",
        "React", "Vue", "SQL", "金融", "风控", "支付", "招聘", "算法", "大模型",
    )
    locations = ("北京", "上海", "深圳", "广州", "杭州", "成都")

    async def parse_jd(self, text: str) -> ParsedJd:
        compact = " ".join(text.split())
        min_years = _extract_experience_years(compact)
        degree = None
        for token, normalized in (
            ("博士", "博士"), ("PhD", "博士"),
            ("硕士", "硕士"), ("本科", "本科"), ("大专", "大专"),
        ):
            if token.casefold() in compact.casefold():
                degree = normalized
                break
        location = next(
            (item for item in self.locations if item.casefold() in compact.casefold()),
            None,
        )
        skills = [
            item for item in self.skill_terms if item.casefold() in compact.casefold()
        ]
        ai_category = None
        if any(t in compact for t in ("大模型", "LLM", "算法", "机器学习", "深度学习")):
            ai_category = "CORE_AI"
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "待识别")
        return ParsedJd(
            title=first_line[:80],
            min_years=min_years,
            highest_degree=degree,
            location=location,
            summary=compact[:500],
            ai_category=ai_category,
        )

    async def split_jds(self, text: str) -> list[str]:
        return split_jd_text(text)
