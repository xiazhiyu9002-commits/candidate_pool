from pathlib import Path
from types import SimpleNamespace

from kerui_recruit.search.contracts import SearchChunk
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex


class FakeTable:
    def __init__(self) -> None:
        self.create_index_calls = 0
        self.optimize_calls = 0

    def delete(self, _predicate: str) -> None:
        pass

    def add(self, _records: list[dict]) -> None:
        pass

    def create_index(self, *_args, **_kwargs) -> None:
        self.create_index_calls += 1

    def optimize(self) -> None:
        self.optimize_calls += 1

    def count_rows(self) -> int:
        return 21


class FakeDatabase:
    def __init__(self) -> None:
        self.table = FakeTable()
        self.exists = False

    def list_tables(self) -> SimpleNamespace:
        return SimpleNamespace(tables=["candidate_chunks"] if self.exists else [])

    def create_table(self, *_args, **_kwargs) -> FakeTable:
        self.exists = True
        return self.table

    def open_table(self, _name: str) -> FakeTable:
        return self.table


def _chunk(index: int) -> SearchChunk:
    return SearchChunk(
        id=f"chunk-{index}",
        candidate_id=f"candidate-{index}",
        revision_id=f"revision-{index}",
        content=f"Python engineer {index}",
        vector=(1.0, 0.0),
        total_years=None,
        highest_degree=None,
        location=None,
        candidate_status="AVAILABLE",
    )


def test_incremental_writes_do_not_rebuild_fts_and_optimize_in_batches(
    tmp_path: Path,
) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    database = FakeDatabase()
    index.database = database

    for value in range(21):
        index.upsert([_chunk(value)])

    assert database.table.create_index_calls == 2
    assert database.table.optimize_calls == 1


def test_pending_partial_index_is_optimized_separately_from_warmup(tmp_path: Path) -> None:
    index = LanceDBSearchIndex(tmp_path / "search", vector_dimension=2)
    database = FakeDatabase()
    database.exists = True
    index.database = database
    index._dirty_marker.touch()

    assert index.warmup() == 21
    assert database.table.optimize_calls == 0
    assert index._dirty_marker.exists()

    assert index.optimize_pending() is True
    assert database.table.optimize_calls == 1
    assert not index._dirty_marker.exists()
