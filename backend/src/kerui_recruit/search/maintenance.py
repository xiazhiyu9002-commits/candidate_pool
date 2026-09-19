"""Index maintenance CLI.

``python -m kerui_recruit.search.maintenance {diagnose,migrate-reuse,validate}``
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from kerui_recruit.search.rebuild_maintenance import (
    diagnose,
    migrate_reusing_vectors,
    validate_index,
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--index-root", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimension", required=True, type=int)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    diagnose_parser = sub.add_parser("diagnose")
    _add_common(diagnose_parser)

    migrate_parser = sub.add_parser("migrate-reuse")
    _add_common(migrate_parser)
    migrate_parser.add_argument("--staging-root", required=True, type=Path)
    migrate_parser.add_argument("--app-stopped", action="store_true")

    validate_parser = sub.add_parser("validate")
    _add_common(validate_parser)
    validate_parser.add_argument("--staging-root", required=True, type=Path)

    args = parser.parse_args(arguments)
    try:
        if args.command == "diagnose":
            result = diagnose(args.database, args.index_root,
                              embedding_model=args.model, dimension=args.dimension)
        elif args.command == "migrate-reuse":
            result = migrate_reusing_vectors(
                args.database, args.index_root, args.staging_root,
                embedding_model=args.model, vector_dimension=args.dimension,
                app_stopped=args.app_stopped)
        else:
            result = validate_index(
                args.database, args.index_root, args.staging_root,
                embedding_model=args.model, vector_dimension=args.dimension)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "FAILED_VALIDATION":
            return 1
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "instruction": "No index was switched. Check inputs, shutdown status and staging path."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
