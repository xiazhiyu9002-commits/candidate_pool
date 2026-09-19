from pathlib import Path

import pytest

from kerui_recruit.bench.search_benchmark import BenchmarkApp


@pytest.fixture
def benchmark_app(tmp_path: Path) -> BenchmarkApp:
    return BenchmarkApp(tmp_path)


@pytest.mark.performance
def test_hybrid_search_scaled_budget(benchmark_app: BenchmarkApp) -> None:
    report = benchmark_app.run(candidate_count=12_000, query_count=200, concurrency=5)

    assert report.error_rate == 0
    assert report.p95_ms <= 1_500
    assert report.p99_ms <= 2_500
    assert report.recall_at_300 >= 0.98