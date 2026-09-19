from pathlib import Path

from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lancedb_index import (
    INDEX_CHUNK_VERSION,
    INDEX_SCHEMA_VERSION,
    LanceDBSearchIndex,
)


class JdSearchIndex:
    """A separate, versioned hybrid projection of open, current, READY jobs.

    The durable sync worker owns eligibility and computes one embedding per JD
    revision. Candidate matching queries this index, never the talent pool.
    """

    def __init__(self, root: Path, *, vector_dimension: int,
                 embedding_model: str = "unspecified", schema_version: str = INDEX_SCHEMA_VERSION,
                 chunk_version: str = INDEX_CHUNK_VERSION) -> None:
        self.index = LanceDBSearchIndex(root, vector_dimension=vector_dimension,
                                       embedding_model=embedding_model,
                                       schema_version=schema_version, chunk_version=chunk_version)

    def upsert(self, *, jd_id: str, revision_id: str, content: str,
               vector: tuple[float, ...] | list[float], company: str = "", title: str = "",
               min_years: float | None = None, highest_degree: str | None = None,
               location: str | None = None, direction: str | None = None,
               career_directions: tuple[str, ...] = (),
               career_specializations: tuple[str, ...] = (),
               business_directions: tuple[str, ...] = ()) -> None:
        self.index.replace_candidate([SearchChunk(
            id=f"jd:{jd_id}:{revision_id}", candidate_id=jd_id, revision_id=revision_id,
            content=content, vector=tuple(vector), total_years=min_years,
            highest_degree=highest_degree, location=location, candidate_status="AVAILABLE",
            direction=direction,
            career_directions=career_directions,
            career_specializations=career_specializations,
            business_directions=business_directions)])

    def upsert_many(self, *, jd_id: str, revision_id: str,
                    documents: list[dict], vectors: list[tuple[float, ...] | list[float]],
                    company: str = "", title: str = "",
                    min_years: float | None = None, highest_degree: str | None = None,
                    location: str | None = None, direction: str | None = None,
                    career_directions: tuple[str, ...] = (),
                    career_specializations: tuple[str, ...] = (),
                    business_directions: tuple[str, ...] = ()) -> None:
        """写入一个 JD 的父/子 chunk（分片检索）。"""
        chunks: list[SearchChunk] = []
        for i, (doc, vector) in enumerate(zip(documents, vectors)):
            chunks.append(SearchChunk(
                id=f"jd:{jd_id}:{revision_id}:{i}",
                candidate_id=jd_id,
                revision_id=revision_id,
                content=doc["keyword_text"],
                vector=tuple(vector),
                total_years=min_years,
                highest_degree=highest_degree,
                location=location,
                candidate_status="AVAILABLE",
                keyword_text=doc.get("keyword_text"),
                keyword_index_text=doc.get("keyword_index_text"),
                vector_text=doc.get("vector_text"),
                chunk_type=doc.get("chunk_type", "parent"),
                parent_id=doc.get("parent_id"),
                direction=direction,
                career_directions=career_directions,
                career_specializations=career_specializations,
                business_directions=business_directions,
            ))
        self.index.replace_candidate(chunks)

    def delete_jd(self, jd_id: str) -> None:
        self.index.delete_candidate(jd_id)

    def is_ready(self) -> bool:
        return self.index.is_ready() and self.index.warmup() > 0
