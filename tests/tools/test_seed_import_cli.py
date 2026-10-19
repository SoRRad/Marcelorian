"""The seed import command: dry run by default, read-only, refuses invalid files whole."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from sqlalchemy import func, select

from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.models import ResearchRecord
from marcelorian.tools.seed_import import main
from tests.seed_fixture import contact, standard_rows, write_csv


def run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(args), out=out)
    return code, out.getvalue()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(db: Path) -> int:
    factory = make_session_factory(make_engine(db))
    with transaction(factory) as s:
        return s.scalar(select(func.count()).select_from(ResearchRecord)) or 0


def test_dry_run_without_a_database_creates_nothing(tmp_path: Path) -> None:
    seed = write_csv(tmp_path / "seed.csv", standard_rows())
    db = tmp_path / "app.db"
    code, text = run(str(seed), "--db", str(db))
    assert code == 0 and not db.exists()
    assert "new: 5" in text and "excluded: 1" in text and "nothing was written" in text


def test_dry_run_on_an_existing_database_does_not_modify_it(tmp_path: Path, db_path: Path) -> None:
    seed = write_csv(tmp_path / "seed.csv", standard_rows())
    assert run(str(seed), "--db", str(db_path), "--commit")[0] == 0
    before = digest(db_path)
    code, text = run(str(seed), "--db", str(db_path))
    assert code == 0 and "unchanged: 5" in text
    assert digest(db_path) == before


def test_commit_then_repeat(tmp_path: Path, db_path: Path) -> None:
    seed = write_csv(tmp_path / "seed.csv", standard_rows())
    code, text = run(str(seed), "--db", str(db_path), "--commit")
    assert code == 0 and "Imported into research staging" in text
    assert count(db_path) == 6
    code, text = run(str(seed), "--db", str(db_path), "--commit")
    assert code == 0 and "unchanged: 5" in text and count(db_path) == 6


def test_invalid_file_is_refused_whole(tmp_path: Path, db_path: Path) -> None:
    seed = write_csv(
        tmp_path / "seed.csv", [*standard_rows(), contact("C-X", send_authorized="true")]
    )
    code, text = run(str(seed), "--db", str(db_path))
    assert code == 1 and "send_authorized_must_be_false" in text
    code, text = run(str(seed), "--db", str(db_path), "--commit")
    assert code == 1 and "nothing was imported" in text
    assert count(db_path) == 0


def test_unreadable_header_is_refused(tmp_path: Path, db_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    code, text = run(str(bad), "--db", str(db_path), "--commit")
    assert code == 2 and "missing columns" in text


def test_dry_run_against_an_unmigrated_database(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "with space" / "old.db"
    db.parent.mkdir()
    sqlite3.connect(db).close()  # an empty database without research staging
    before = digest(db)
    seed = write_csv(tmp_path / "seed.csv", standard_rows())
    code, text = run(str(seed), "--db", str(db))
    assert code == 0 and "new: 5" in text and digest(db) == before


def test_dry_run_reads_staging_in_a_path_with_spaces(tmp_path: Path, db_path: Path) -> None:
    import shutil

    spaced = tmp_path / "Program Data" / "app.db"
    spaced.parent.mkdir()
    shutil.copy(db_path, spaced)
    seed = write_csv(tmp_path / "seed.csv", standard_rows())
    assert run(str(seed), "--db", str(spaced), "--commit")[0] == 0
    code, text = run(str(seed), "--db", str(spaced))
    assert code == 0 and "unchanged: 5" in text
