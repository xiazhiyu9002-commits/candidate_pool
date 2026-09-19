from __future__ import annotations

import json
from pathlib import Path

import pymupdf
from docx import Document

from kerui_recruit.bench.corpus_acceptance import AcceptanceCheckpoint, scan_corpus


def _write_pdf(path: Path, text: str | None) -> None:
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _write_docx(path: Path, text: str) -> None:
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def test_scan_corpus_reports_only_aggregate_metrics(tmp_path: Path) -> None:
    source = tmp_path / "private-corpus"
    source.mkdir()
    _write_pdf(source / "candidate-secret.pdf", "Python engineer with six years experience")
    (source / "duplicate-secret.pdf").write_bytes((source / "candidate-secret.pdf").read_bytes())
    _write_pdf(source / "scan-secret.pdf", None)
    _write_docx(source / "resume-secret.docx", "Java engineer with five years experience")
    (source / "photo-secret.png").write_bytes(b"not-a-resume")

    inventory = scan_corpus(source)
    report = inventory.to_dict()
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["total_files"] == 5
    assert report["supported_files"] == 4
    assert report["unsupported_files"] == 1
    assert report["duplicate_files"] == 1
    assert report["extractable_files"] == 3
    assert report["ocr_required_files"] == 1
    assert report["extraction_failed_files"] == 0
    assert report["formats"]["pdf"]["files"] == 3
    assert report["formats"]["docx"]["files"] == 1
    assert len(report["aggregate_sha256"]) == 64
    assert "candidate-secret" not in serialized
    assert "Python engineer" not in serialized
    assert str(source) not in serialized


def test_scan_corpus_is_deterministic_and_does_not_modify_sources(tmp_path: Path) -> None:
    source = tmp_path / "corpus"
    source.mkdir()
    _write_docx(source / "resume.docx", "Data engineer experience")
    before = (source / "resume.docx").read_bytes()

    first = scan_corpus(source).to_dict()
    second = scan_corpus(source).to_dict()

    assert first == second
    assert (source / "resume.docx").read_bytes() == before


def test_acceptance_checkpoint_resumes_without_storing_paths_or_text(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = AcceptanceCheckpoint.open(checkpoint_path, "a" * 64)

    checkpoint.record("b" * 64, outcome="SUCCESS", latency_ms=125)
    resumed = AcceptanceCheckpoint.open(checkpoint_path, "a" * 64)
    serialized = checkpoint_path.read_text(encoding="utf-8")

    assert resumed.contains("b" * 64)
    assert resumed.outcomes == {"SUCCESS": 1}
    assert resumed.latency_ms_total == 125
    assert "resume.pdf" not in serialized
    assert "candidate text" not in serialized


def test_acceptance_checkpoint_rejects_a_different_corpus(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    AcceptanceCheckpoint.open(checkpoint_path, "a" * 64).record(
        "b" * 64,
        outcome="SUCCESS",
        latency_ms=10,
    )

    try:
        AcceptanceCheckpoint.open(checkpoint_path, "c" * 64)
    except ValueError as error:
        assert "different corpus" in str(error)
    else:
        raise AssertionError("expected a corpus identity mismatch")
