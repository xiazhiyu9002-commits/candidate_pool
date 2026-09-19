"""索引升级编排：重置重建的进度落盘与归档清理。"""
import json
from pathlib import Path

from kerui_recruit.search.upgrade import IndexRebuildTracker


def test_tracker_finishes_and_discards_archive(tmp_path: Path):
    archive = tmp_path / "search.pre-rebuild-20260101T000000Z"
    archive.mkdir()
    (archive / "candidate-index-metadata.json").write_text("{}", encoding="utf-8")

    tracker = IndexRebuildTracker(tmp_path / "index-rebuild.json")
    tracker.begin(mode="reset", reason="向量维度已更换", total=3, archive=archive)
    assert tracker.active is True

    # 队列未排空：不落完成时间，归档保留。
    tracker.poll(pending=2)
    assert tracker.snapshot()["finished_at"] is None
    assert archive.exists()

    tracker.poll(pending=0)
    assert tracker.snapshot()["finished_at"] is not None
    assert tracker.active is False
    assert not archive.exists()


def test_tracker_resumes_unfinished_rebuild_from_disk(tmp_path: Path):
    """中途退出应用后，下次启动读到未完成状态即可继续收尾。"""
    state_path = tmp_path / "index-rebuild.json"
    state_path.write_text(
        json.dumps({"mode": "reset", "reason": "向量维度已更换", "total": 5,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": None, "archived": None}),
        encoding="utf-8",
    )

    resumed = IndexRebuildTracker(state_path)
    assert resumed.active is True
    resumed.poll(pending=0)
    assert resumed.active is False


def test_tracker_without_state_is_inactive(tmp_path: Path):
    tracker = IndexRebuildTracker(tmp_path / "index-rebuild.json")
    assert tracker.snapshot() is None
    assert tracker.active is False


def test_plan_index_upgrade_tolerates_bom(tmp_path: Path):
    """带 BOM 的 metadata（Windows 编辑器/脚本常见）必须按版本落后处理。

    否则一次「可原地修复」的升级会被误判成索引损坏，白白丢弃整个索引。
    """
    from kerui_recruit.search.upgrade import INPLACE, plan_index_upgrade

    search = tmp_path / "search"
    search.mkdir()
    payload = json.dumps({"schema_version": "8", "embedding_model": "local-hash-v1",
                          "vector_dimension": 64, "chunk_version": "6"}).encode("utf-8")
    (search / "candidate-index-metadata.json").write_bytes(b"\xef\xbb\xbf" + payload)

    plan = plan_index_upgrade(search, embedding_model="local-hash-v1", vector_dimension=64)
    assert plan is not None and plan.mode == INPLACE
