from io import BytesIO
from pathlib import Path

from kerui_recruit.storage.blobs import BlobStore


def test_blob_store_deduplicates_content_and_uses_hash_shards(tmp_path: Path) -> None:
    """Writing duplicate bytes twice must not consume duplicate original storage."""
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")

    first = store.put(BytesIO(b"resume"), ".pdf")
    second = store.put(BytesIO(b"resume"), ".pdf")

    expected = (
        tmp_path
        / "blobs"
        / first.sha256[:2]
        / first.sha256[2:4]
        / f"{first.sha256}.pdf"
    )
    assert first.path == expected
    assert second.path == expected
    assert first.created is True
    assert second.created is False
    assert list((tmp_path / "blobs").rglob("*.pdf")) == [expected]
    assert expected.read_bytes() == b"resume"
    assert list((tmp_path / "temp").iterdir()) == []


def test_blob_store_keeps_distinct_suffixes_for_distinct_content(tmp_path: Path) -> None:
    """Using a fixed suffix would make original downloads lose their file type."""
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")

    pdf = store.put(BytesIO(b"pdf"), ".PDF")
    docx = store.put(BytesIO(b"docx"), "docx")

    assert pdf.path.suffix == ".pdf"
    assert docx.path.suffix == ".docx"
    assert pdf.sha256 != docx.sha256


def test_blob_store_reopens_content_by_hash_and_suffix(tmp_path: Path) -> None:
    """A stored original must remain retrievable without exposing its physical path."""
    store = BlobStore(tmp_path / "blobs", tmp_path / "temp")
    stored = store.put(BytesIO(b"original"), ".pdf")

    with store.open(stored.sha256, stored.suffix) as original:
        assert original.read() == b"original"
