"""Programmatic Alembic upgrade (bundled migrations; spec §2, §17).

SQLite cannot alter constraints in place, so migrations rebuild tables in batch mode
(copy, drop, rename). With foreign keys enforced, dropping a table that other rows
reference fails, so migrations follow SQLite's documented rebuild procedure: foreign
keys off for the connection (set outside any transaction), migrate, verify with
``PRAGMA foreign_key_check`` before committing, and turn enforcement back on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine

_HERE = Path(__file__).resolve().parent


class MigrationIntegrityError(RuntimeError):
    """A migration left rows whose foreign keys point nowhere; nothing was committed."""


def alembic_config() -> Config:
    config = Config(str(_HERE / "alembic.ini"))
    config.set_main_option("script_location", str(_HERE / "migrations"))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database. P4 adds an online-API backup before each migration."""
    _run(engine, lambda config: command.upgrade(config, revision))


def downgrade(engine: Engine, revision: str) -> None:
    _run(engine, lambda config: command.downgrade(config, revision))


def _run(engine: Engine, step: Any) -> None:
    config = alembic_config()
    with engine.connect() as connection:
        raw = _dbapi(connection)
        raw.execute("PRAGMA foreign_keys=OFF")  # no transaction is open yet: takes effect
        try:
            config.attributes["connection"] = connection
            step(config)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                connection.rollback()
                raise MigrationIntegrityError(f"foreign key violations: {violations[:5]}")
            connection.commit()
        finally:
            raw.execute("PRAGMA foreign_keys=ON")


def _dbapi(connection: Connection) -> Any:
    raw = connection.connection.dbapi_connection
    if raw is None:
        raise RuntimeError("no DBAPI connection")
    return raw
