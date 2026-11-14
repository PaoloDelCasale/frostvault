"""The schema guard of ADR-0035, with the linear migrations of ADR-0041.

``0001_baseline`` froze when the Owner created the production catalog; since
then ``SCHEMA_REVISIONS`` is the linear chain of Alembic revisions, ending at
``SCHEMA_HEAD``. Before the app serves traffic, and before a restored catalog
is touched, a database is one of:

- empty: it holds no object at all, and is initialized with Alembic;
- current: ``alembic_version`` holds exactly ``SCHEMA_HEAD``;
- outdated: ``alembic_version`` holds exactly one earlier revision of the
  chain; it is upgraded in place to ``SCHEMA_HEAD`` with the chain's own
  migrations;
- anything else (a v0.5.x or unknown revision, several, or objects without
  ``alembic_version``): refused before any write.

A catalog outside the chain is never stamped, upgraded, reset or adopted.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import tuple_row


# Oldest first; every revision's down_revision is the one before it.
SCHEMA_REVISIONS = ("0001_baseline", "0002_file_ownership")
SCHEMA_HEAD = SCHEMA_REVISIONS[-1]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# Serializes concurrent starts on an empty database ("FVSCHEMA" in ASCII).
_INITIALIZE_LOCK_KEY = 0x4656534348454D41

_REMEDY = (
    "FrostVault upgrades only a catalog of an earlier v0.6+ release and never "
    "stamps or adopts another schema: point it at an empty PostgreSQL "
    "database, or restore a metadata backup made by a v0.6+ release."
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
    """The database is refused: it is neither empty nor in ``SCHEMA_REVISIONS``."""


class SchemaState(Enum):
    EMPTY = "empty"
    CURRENT = "current"
    OUTDATED = "outdated"


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
        if len(revisions) == 1 and revisions[0] in SCHEMA_REVISIONS:
            return SchemaState.OUTDATED
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
    state = inspect_schema(connection)
    if state is SchemaState.EMPTY:
        raise DatabaseSchemaError(
            f"Database refused: it is empty, this operation requires a catalog "
            f"at {SCHEMA_HEAD}."
        )
    if state is SchemaState.OUTDATED:
        raise DatabaseSchemaError(
            f"Database refused: it is at an earlier revision, this operation "
            f"requires a catalog at {SCHEMA_HEAD}. Upgrade it first with "
            f"app.schema_guard.upgrade_schema (the app does it at start)."
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


def prepare_schema(dsn: str, *, initialize_empty: bool = True) -> SchemaState:
    """Initialize an empty database, upgrade an outdated one, accept a current one.

    Classification and migration share one transaction under an advisory
    lock, so concurrent starts initialize or upgrade a database exactly once
    and a failed migration leaves it as it was (PostgreSQL DDL is
    transactional). Returns the state found before any migration. With
    ``initialize_empty=False`` an empty database is refused, not initialized.
    """
    engine = _engine(dsn)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_INITIALIZE_LOCK_KEY})")
            state = inspect_schema(connection)
            if state is SchemaState.EMPTY and not initialize_empty:
                raise DatabaseSchemaError(
                    f"Database refused: it is empty, this operation requires a "
                    f"catalog at {SCHEMA_HEAD}."
                )
            if state is not SchemaState.CURRENT:
                _alembic_upgrade(connection)
                if inspect_schema(connection) is not SchemaState.CURRENT:
                    raise DatabaseSchemaError(
                        f"Database migration did not reach {SCHEMA_HEAD}"
                    )
            return state
    finally:
        engine.dispose()


def upgrade_schema(dsn: str) -> SchemaState:
    """Bring a restored catalog of an earlier v0.6+ release to ``SCHEMA_HEAD``.

    Used by the manual metadata restore before ``record_metadata_restore``:
    it never initializes an empty database and refuses anything outside the
    chain, exactly like the start.
    """
    return prepare_schema(dsn, initialize_empty=False)
