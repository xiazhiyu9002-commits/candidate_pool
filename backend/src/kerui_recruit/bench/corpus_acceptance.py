from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from kerui_recruit.resumes.extract import extract_text


SUPPORTED_SUFFIXES = frozenset({".pdf", ".doc", ".docx"})


@dataclass(frozen=True, slots=True)
class FormatMetrics:
    files: int = 0
    bytes: int = 0
    extractable: int = 0
    ocr_required: int = 0
    extraction_failed: int = 0


@dataclass(frozen=True, slots=True)
class CorpusInventory:
    total_files: int
    total_bytes: int
    supported_files: int
    unsupported_files: int
    duplicate_files: int
    extractable_files: int
    ocr_required_files: int
    extraction_failed_files: int
    aggregate_sha256: str
    formats: dict[str, FormatMetrics]

    def to_dict(self) -> dict[str, object]:
        return {
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "supported_files": self.supported_files,
            "unsupported_files": self.unsupported_files,
            "duplicate_files": self.duplicate_files,
            "extractable_files": self.extractable_files,
            "ocr_required_files": self.ocr_required_files,
            "extraction_failed_files": self.extraction_failed_files,
            "aggregate_sha256": self.aggregate_sha256,
            "formats": {
                name: asdict(metrics)
                for name, metrics in sorted(self.formats.items())
            },
        }


@dataclass(slots=True)
class AcceptanceCheckpoint:
    path: Path
    corpus_sha256: str
    processed_sha256: set[str]
    outcomes: dict[str, int]
    latency_ms_total: int

    @classmethod
    def open(cls, path: Path, corpus_sha256: str) -> AcceptanceCheckpoint:
        if not path.exists():
            return cls(path, corpus_sha256, set(), {}, 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("corpus_sha256") != corpus_sha256:
            raise ValueError("checkpoint belongs to a different corpus")
        return cls(
            path=path,
            corpus_sha256=corpus_sha256,
            processed_sha256=set(data.get("processed_sha256", [])),
            outcomes={str(key): int(value) for key, value in data.get("outcomes", {}).items()},
            latency_ms_total=int(data.get("latency_ms_total", 0)),
        )

    def contains(self, content_sha256: str) -> bool:
        return content_sha256 in self.processed_sha256

    def record(self, content_sha256: str, *, outcome: str, latency_ms: int) -> None:
        if self.contains(content_sha256):
            return
        self.processed_sha256.add(content_sha256)
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.latency_ms_total += max(0, latency_ms)
        self._save()

    def _save(self) -> None:
        payload = {
            "corpus_sha256": self.corpus_sha256,
            "processed_sha256": sorted(self.processed_sha256),
            "outcomes": dict(sorted(self.outcomes.items())),
            "latency_ms_total": self.latency_ms_total,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def scan_corpus(root: Path) -> CorpusInventory:
    """Read a corpus without mutating it and return privacy-safe aggregates."""
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError("corpus root must be an accessible directory")

    paths = sorted((path for path in resolved.rglob("*") if path.is_file()))
    content_hashes: list[str] = []
    seen_hashes: set[str] = set()
    duplicate_files = 0
    formats: dict[str, FormatMetrics] = {}
    supported_files = 0
    extractable_files = 0
    ocr_required_files = 0
    extraction_failed_files = 0
    total_bytes = 0

    for path in paths:
        size = path.stat().st_size
        total_bytes += size
        content_hash = _sha256(path)
        content_hashes.append(content_hash)
        if content_hash in seen_hashes:
            duplicate_files += 1
        seen_hashes.add(content_hash)

        suffix = path.suffix.lower()
        format_name = suffix.removeprefix(".") or "no_extension"
        current = formats.get(format_name, FormatMetrics())
        extractable = 0
        ocr_required = 0
        extraction_failed = 0

        if suffix in SUPPORTED_SUFFIXES:
            supported_files += 1
            try:
                extracted = extract_text(path)
                if extracted.requires_ocr:
                    ocr_required = 1
                    ocr_required_files += 1
                else:
                    extractable = 1
                    extractable_files += 1
            except Exception:  # noqa: BLE001 - aggregate failures without leaking paths
                extraction_failed = 1
                extraction_failed_files += 1

        formats[format_name] = FormatMetrics(
            files=current.files + 1,
            bytes=current.bytes + size,
            extractable=current.extractable + extractable,
            ocr_required=current.ocr_required + ocr_required,
            extraction_failed=current.extraction_failed + extraction_failed,
        )

    aggregate = hashlib.sha256("\n".join(sorted(content_hashes)).encode()).hexdigest()
    return CorpusInventory(
        total_files=len(paths),
        total_bytes=total_bytes,
        supported_files=supported_files,
        unsupported_files=len(paths) - supported_files,
        duplicate_files=duplicate_files,
        extractable_files=extractable_files,
        ocr_required_files=ocr_required_files,
        extraction_failed_files=extraction_failed_files,
        aggregate_sha256=aggregate,
        formats=formats,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a privacy-safe resume corpus inventory")
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(scan_corpus(args.root).to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
