from __future__ import annotations

import hashlib
import math


class FakeEmbeddingProvider:
    def __init__(self, *, dimension: int = 32) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
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


class FakeRerankerProvider:
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        terms = tuple(term.casefold() for term in query.split() if term)
        scored = [
            (sum(document.casefold().count(term) for term in terms), index)
            for index, document in enumerate(documents)
        ]
        return [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]


class FakeOCRProvider:
    """Deterministic OCR stub returning a fixed transcript."""

    async def extract(self, content: bytes, filename: str) -> str:
        return "张三 本科 6年 Java Python"

    async def extract_pages(
        self, content: bytes, filename: str, page_indexes: list[int]
    ) -> list[str]:
        return ["张三 本科 6年 Java Python"] * len(page_indexes)
