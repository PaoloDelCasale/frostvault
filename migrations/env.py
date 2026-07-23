from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> str:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("database_url"):
        return arguments["database_url"]
    if os.getenv("DB_BACKEND", "postgresql").strip().lower() == "sqlite":
        path = os.getenv("SQLITE_PATH", "/data/frostvault.db")
        return f"sqlite:///{path}"
    user = quote_plus(os.getenv("PGUSER", "postgres"))
    password = quote_plus(os.getenv("PGPASSWORD", ""))
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    database = quote_plus(os.getenv("PGDATABASE", user))
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


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
    configuration["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
