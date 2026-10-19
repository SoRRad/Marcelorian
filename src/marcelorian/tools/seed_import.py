"""Preview or import a research-catalog seed CSV into research staging (docs/SEED_IMPORT.md).

Dry run by default: the database is opened read-only and nothing is written. Pass
``--commit`` to stage the file. Either way the tool makes no network calls and never
creates PIs, addresses, campaigns, drafts, approvals or reservations.

    python -m marcelorian.tools.seed_import data/seeds/marcelorian_ophthalmology_seed.csv
    python -m marcelorian.tools.seed_import data/seeds/marcelorian_ophthalmology_seed.csv --commit
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import TextIO

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from marcelorian.clock import SystemClock
from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.migrate import upgrade
from marcelorian.domain.enums import Actor
from marcelorian.domain.seed import ParsedSeed, SeedFormatError, read_seed_file
from marcelorian.services.research_seed import SeedPreview, SeedRejected, import_seed, preview

MAX_LISTED_ERRORS = 25


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dry_run(parsed: ParsedSeed, db_path: Path) -> SeedPreview:
    """Classify against what ``db_path`` has staged, opening it read-only."""
    if db_path.exists():
        uri = f"{db_path.resolve().as_uri()}?mode=ro"  # percent-encoded; Windows-safe
        engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
        try:
            with Session(engine) as session:
                return preview(session, parsed)
        except OperationalError:
            pass  # not migrated to research staging yet: nothing is staged
        finally:
            engine.dispose()
    empty = make_engine(":memory:")
    upgrade(empty)
    with Session(empty) as session:
        return preview(session, parsed)


def commit(parsed: ParsedSeed, db_path: Path, source: Path) -> SeedPreview:
    engine = make_engine(db_path)
    try:
        upgrade(engine)
        factory = make_session_factory(engine)
        with transaction(factory) as session:
            return import_seed(
                session,
                parsed,
                source_name=source.name,
                file_sha256=file_sha256(source),
                now=SystemClock().now(),
                actor=Actor.APPLICANT,
            )
    finally:
        engine.dispose()


def report(result: SeedPreview, out: TextIO, *, committed: bool) -> None:
    out.write(f"Rows read: {result.row_count}\n")
    out.write(f"Snapshot date(s): {', '.join(result.snapshot_dates) or '-'}\n")
    out.write(f"Record types: {result.record_types}\n")
    for name, count in result.counts().items():
        out.write(f"  {name:>16}: {count}\n")
    if result.unknown_columns:
        out.write(f"Unknown columns kept with each record: {', '.join(result.unknown_columns)}\n")
    for error in result.invalid[:MAX_LISTED_ERRORS]:
        out.write(f"  invalid line {error.line or '-'} {error.record_id or '-'}: {error.reason}\n")
    if len(result.invalid) > MAX_LISTED_ERRORS:
        out.write(f"  ... and {len(result.invalid) - MAX_LISTED_ERRORS} more\n")
    if committed:
        out.write(f"Imported into research staging (import #{result.import_id}).\n")
    elif result.ok:
        out.write("Dry run: nothing was written. Re-run with --commit to stage.\n")
    else:
        out.write("Invalid rows: the file would be refused as a whole.\n")
    out.write("No email, draft, campaign, approval or reservation was created.\n")


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--db", type=Path, default=None, help="default: the app database")
    parser.add_argument("--commit", action="store_true", help="write to research staging")
    args = parser.parse_args(argv)
    db_path: Path = args.db
    if db_path is None:
        from marcelorian.platform.paths import resolve_app_paths

        db_path = resolve_app_paths().db_path
    try:
        parsed = read_seed_file(str(args.csv))
    except (SeedFormatError, UnicodeDecodeError, OSError) as exc:
        out.write(f"Refused: {exc}\n")
        return 2
    if not args.commit:
        result = dry_run(parsed, db_path)
        report(result, out, committed=False)
        return 0 if result.ok else 1
    try:
        result = commit(parsed, db_path, args.csv)
    except SeedRejected as exc:
        report(exc.preview, out, committed=False)
        out.write("Refused: nothing was imported.\n")
        return 1
    report(result, out, committed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
