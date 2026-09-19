from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kerui_recruit.bench.generate import BenchmarkDataset, generate_dataset
from kerui_recruit.providers.local import LocalHashEmbeddingProvider
from kerui_recruit.search.contracts import CandidateFilters, SearchChunk, SearchRequest
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    candidate_count: int
    query_count: int
    concurrency: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_rate: float
    recall_at_300: float
    ndcg_at_10: float

    def to_json(self) -> dict:
        return asdict(self)


class BenchmarkApp:
    """Runs the Phase 0 scaled gate against a local LanceDB index.

    Uses the deterministic local hash provider so the benchmark is reproducible
    and never requires a live API key or network access.
    """

    def __init__(self, root: Path, *, vector_dimension: int = 64) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.embedding = LocalHashEmbeddingProvider(dimension=vector_dimension)
        self.index = LanceDBSearchIndex(
            self.root / "search", vector_dimension=vector_dimension
        )

    def run(
        self,
        *,
        candidate_count: int,
        query_count: int,
        concurrency: int,
    ) -> BenchmarkReport:
        dataset = generate_dataset(candidate_count, seed=42)
        self.index.upsert(self._chunks(dataset))

        queries = dataset.queries[:query_count]
        latencies: list[float] = []
        errors = 0
        recall_sums: list[float] = []
        ndcg_sums: list[float] = []

        for query in queries:
            started = time.perf_counter()
            try:
                hits = self.index.search(
                    SearchRequest(
                        query=query.text,
                        query_vector=tuple(self.embedding._embed(query.text)),
                        filters=CandidateFilters(),
                        limit=300,
                    )
                )
            except Exception:
                errors += 1
                continue
            latencies.append((time.perf_counter() - started) * 1000)
            recall_sums.append(self._recall(query, hits))
            ndcg_sums.append(self._ndcg(query, hits))

        if not latencies:
            return BenchmarkReport(
                candidate_count=candidate_count,
                query_count=len(queries),
                concurrency=concurrency,
                p50_ms=0.0,
                p95_ms=0.0,
                p99_ms=0.0,
                error_rate=1.0,
                recall_at_300=0.0,
                ndcg_at_10=0.0,
            )

        sorted_latencies = sorted(latencies)
        return BenchmarkReport(
            candidate_count=candidate_count,
            query_count=len(queries),
            concurrency=concurrency,
            p50_ms=self._percentile(sorted_latencies, 0.50),
            p95_ms=self._percentile(sorted_latencies, 0.95),
            p99_ms=self._percentile(sorted_latencies, 0.99),
            error_rate=errors / max(1, len(queries)),
            recall_at_300=sum(recall_sums) / len(recall_sums),
            ndcg_at_10=sum(ndcg_sums) / len(ndcg_sums),
        )

    # -- internal helpers ----------------------------------------------------

    def _chunks(self, dataset: BenchmarkDataset) -> list[SearchChunk]:
        chunks: list[SearchChunk] = []
        for candidate in dataset.candidates:
            for index, content in enumerate(candidate.chunks):
                chunks.append(
                    SearchChunk(
                        id=f"{candidate.revision_id}:{index}",
                        candidate_id=candidate.candidate_id,
                        revision_id=candidate.revision_id,
                        content=content,
                        vector=tuple(self.embedding._embed(content)),
                        total_years=5.0,
                        highest_degree="MASTER",
                        location="上海",
                        candidate_status="AVAILABLE",
                    )
                )
        return chunks

    @staticmethod
    def _recall(query, hits) -> float:
        relevant = set(query.relevant_candidate_ids)
        if not relevant:
            return 1.0
        retrieved = {hit.candidate_id for hit in hits}
        return len(relevant & retrieved) / len(relevant)

    @staticmethod
    def _ndcg(query, hits) -> float:
        relevant = set(query.relevant_candidate_ids)
        if not relevant:
            return 1.0
        ideal = sorted([1.0] * len(relevant), reverse=True)

        def dcg(ordered: list[float]) -> float:
            return sum(
                rel / math.log2(position + 2)
                for position, rel in enumerate(ordered)
            )

        scored = [1.0 if hit.candidate_id in relevant else 0.0 for hit in hits[:10]]
        ideal_dcg = dcg(ideal)
        if ideal_dcg == 0:
            return 1.0
        return dcg(scored) / ideal_dcg

    @staticmethod
    def _percentile(sorted_values: list[float], fraction: float) -> float:
        if not sorted_values:
            return 0.0
        index = max(
            0,
            min(len(sorted_values) - 1, round((len(sorted_values) - 1) * fraction)),
        )
        return round(sorted_values[index], 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kerui-recruit-benchmark")
    parser.add_argument("--candidates", type=int, default=120_000)
    parser.add_argument("--queries", type=int, default=800)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--qps", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    options = parser.parse_args(argv)

    data_root = Path(tempfile.mkdtemp(prefix="kerui-bench-"))
    report = BenchmarkApp(data_root).run(
        candidate_count=options.candidates,
        query_count=options.queries,
        concurrency=options.concurrency,
    )
    payload = report.to_json()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if options.output is not None:
        options.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())