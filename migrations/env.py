from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import URL, engine_from_config, pool

from app.schema_state import DatabaseSchemaError, read_schema_revision


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> URL:
    arguments = context.get_x_argument(as_dictionary=True)
    dsn = arguments.get("database_url") or os.getenv("DATABASE_URL", "")
    # libpq supplies omitted values from PG*, including socket paths and SSL.
    values = conninfo_to_dict(dsn.replace("postgresql+psycopg://", "postgresql://", 1))
    return URL.create("postgresql+psycopg", query=values)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url().render_as_string(hide_password=False)
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            # Run before Alembic creates/stamps its version table or executes
            # DDL, including direct CLI invocations (not only application start).
            # The raw driver's tuple rows are supported without mutating its
            # row factory underneath SQLAlchemy.
            revision = read_schema_revision(connection.connection.driver_connection)
            scripts = ScriptDirectory.from_config(config)
            if len(scripts.get_heads()) != 1:
                raise DatabaseSchemaError("The local Alembic migration graph is divergent; mutation is blocked.")
            if revision is not None:
                try:
                    known = scripts.get_revision(revision)
                except (ResolutionError, CommandError):
                    known = None
                if known is None:
                    raise DatabaseSchemaError(
                        "The database schema revision is unknown to this application build; "
                        "keep the catalog unchanged and use its matching release."
                    ) from None
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
