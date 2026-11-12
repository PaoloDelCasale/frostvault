"""The simple schema guard of ADR-0035.

One Alembic revision, ``SCHEMA_HEAD``, holds the whole schema. Before the app
serves traffic, and before a restored catalog is touched, a database is one of:

- empty: it holds no object at all, and is initialized with Alembic;
- current: ``alembic_version`` holds exactly ``SCHEMA_HEAD``;
- anything else (another revision, several, or objects without
  ``alembic_version``): refused before any write.

An old catalog is never stamped, upgraded, reset or adopted.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import tuple_row


SCHEMA_HEAD = "0001_baseline"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# Serializes concurrent starts on an empty database ("FVSCHEMA" in ASCII).
_INITIALIZE_LOCK_KEY = 0x4656534348454D41

_REMEDY = (
    "FrostVault never upgrades, stamps or adopts another schema: point it at "
    "an empty PostgreSQL database, or restore a metadata backup made by this "
    "release."
)

# Objects a fresh PostgreSQL database never holds outside the system schemas.
# Extension members (CREATE EXTENSION) are not application data.
_NONEMPTY_SQL = """
WITH user_namespace AS (
    SELECT oid FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema')
      AND NOT starts_with(nspname, 'pg_toast')
      AND NOT starts_with(nspname, 'pg_temp_')
), extension_member AS (
    SELECT classid, objid FROM pg_depend WHERE deptype = 'e'
)
SELECT
    EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.relnamespace IN (SELECT oid FROM user_namespace)
          AND (c.tableoid, c.oid) NOT IN (SELECT classid, objid FROM extension_member)
    )
    OR EXISTS (
        SELECT 1 FROM pg_proc p
        WHERE p.pronamespace IN (SELECT oid FROM user_namespace)
          AND (p.tableoid, p.oid) NOT IN (SELECT classid, objid FROM extension_member)
    )
    OR EXISTS (
        SELECT 1 FROM pg_type t
        WHERE t.typnamespace IN (SELECT oid FROM user_namespace)
          AND t.typtype IN ('d', 'e', 'r', 'm')
          AND (t.tableoid, t.oid) NOT IN (SELECT classid, objid FROM extension_member)
    )
"""

_VERSION_TABLE_SQL = """
SELECT EXISTS (
    SELECT 1 FROM pg_class
    WHERE relname = 'alembic_version'
      AND relnamespace = current_schema()::regnamespace
)
"""


class DatabaseSchemaError(RuntimeError):
    """The database is refused: it is neither empty nor at ``SCHEMA_HEAD``."""


class SchemaState(Enum):
    EMPTY = "empty"
    CURRENT = "current"


def _rows(connection: Any, statement: str) -> list[tuple]:
    """Run a parameterless read on a psycopg or SQLAlchemy connection."""
    from sqlalchemy.engine import Connection

    if isinstance(connection, Connection):
        return [tuple(row) for row in connection.exec_driver_sql(statement).fetchall()]
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(statement)
        return cursor.fetchall()


def inspect_schema(connection: Any) -> SchemaState:
    """Classify the database, or raise ``DatabaseSchemaError``. Never writes."""
    if _rows(connection, _VERSION_TABLE_SQL)[0][0]:
        revisions = [row[0] for row in _rows(connection, "SELECT version_num FROM alembic_version")]
        if revisions == [SCHEMA_HEAD]:
            return SchemaState.CURRENT
        found = ", ".join(sorted(map(str, revisions))) or "no revision"
        raise DatabaseSchemaError(
            f"Database refused: its schema revision is {found}, this release "
            f"requires {SCHEMA_HEAD}. {_REMEDY}"
        )
    if _rows(connection, _NONEMPTY_SQL)[0][0]:
        raise DatabaseSchemaError(
            "Database refused: it is not empty and has no alembic_version "
            f"(this release requires {SCHEMA_HEAD}). {_REMEDY}"
        )
    return SchemaState.EMPTY


def require_current_schema(connection: Any) -> None:
    """Refuse, before any write, a database that is not at ``SCHEMA_HEAD``."""
    if inspect_schema(connection) is not SchemaState.CURRENT:
        raise DatabaseSchemaError(
            f"Database refused: it is empty, this operation requires a catalog "
            f"at {SCHEMA_HEAD}."
        )


def _engine(dsn: str):
    from sqlalchemy import URL, create_engine
    from sqlalchemy.pool import NullPool

    values = conninfo_to_dict(dsn.replace("postgresql+psycopg://", "postgresql://", 1))
    # libpq supplies omitted values from PG*, as for every other connection.
    return create_engine(URL.create("postgresql+psycopg", query=values), poolclass=NullPool)


def _alembic_upgrade(connection: Any) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    # migrations/env.py runs on this connection, inside its open transaction.
    config.attributes["connection"] = connection
    command.upgrade(config, SCHEMA_HEAD)


def prepare_schema(dsn: str) -> SchemaState:
    """Initialize an empty database or accept a current one; refuse the rest.

    Classification and initialization share one transaction under an
    advisory lock, so concurrent starts on an empty database initialize it
    exactly once and a failed initialization leaves it empty. Returns the
    state found before any initialization.
    """
    engine = _engine(dsn)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_INITIALIZE_LOCK_KEY})")
            state = inspect_schema(connection)
            if state is SchemaState.EMPTY:
                _alembic_upgrade(connection)
                if inspect_schema(connection) is not SchemaState.CURRENT:
                    raise DatabaseSchemaError(
                        f"Database initialization did not reach {SCHEMA_HEAD}"
                    )
            return state
    finally:
        engine.dispose()
