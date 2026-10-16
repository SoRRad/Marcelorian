"""Programmatic Alembic upgrade (bundled migrations; spec §2, §17)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

_HERE = Path(__file__).resolve().parent


def alembic_config() -> Config:
    config = Config(str(_HERE / "alembic.ini"))
    config.set_main_option("script_location", str(_HERE / "migrations"))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database. P4 adds an online-API backup before each migration."""
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        connection.commit()
