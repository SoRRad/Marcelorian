"""Alembic environment. Migrations run on the connection handed over by
``marcelorian.db.migrate`` so they use the same pragmas and BEGIN IMMEDIATE."""

from __future__ import annotations

from alembic import context

from marcelorian.db.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("run migrations through marcelorian.db.migrate.upgrade()")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
run_migrations_online()
