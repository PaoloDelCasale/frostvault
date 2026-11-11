"""Read-only catalog preflight, shared by runtime and raw Alembic entrypoints.

No configuration, connections, DDL, stamping or reset lives here. An absent (or
empty) version table does not make an existing catalog a fresh database.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DatabaseSchemaError(RuntimeError):
    """Catalog identity could not be established without guessing."""


def _value(row: Any, key: str) -> Any:
    # Runtime psycopg uses dict_row; Alembic's driver uses tuple rows. Avoid
    # changing that driver's row factory underneath SQLAlchemy.
    return row[key] if isinstance(row, Mapping) else row[0]


def _require_empty_catalog(connection: Any) -> None:
    # Include relations, routines and standalone types in every user schema,
    # not only public/search_path. Even a catalog with empty application tables
    # is not fresh. An empty Alembic bookkeeping table alone is harmless.
    row = connection.execute("""SELECT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE left(n.nspname,3) <> 'pg_' AND n.nspname <> 'information_schema'
          AND NOT (c.oid=COALESCE(to_regclass('alembic_version')::oid,0::oid)
              OR c.oid IN (SELECT indexrelid FROM pg_index
                  WHERE indrelid=to_regclass('alembic_version')))
        UNION ALL
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE left(n.nspname,3) <> 'pg_' AND n.nspname <> 'information_schema'
        UNION ALL
        SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE left(n.nspname,3) <> 'pg_' AND n.nspname <> 'information_schema'
          AND t.typrelid=0 AND t.typelem=0
    ) AS nonempty""").fetchone()
    if _value(row, "nonempty"):
        raise DatabaseSchemaError(
            "Unversioned nonempty catalog: initialization is blocked. "
            "Keep this database unchanged and use its matching application "
            "release or a compatible metadata backup; never force-stamp or reset it."
        )


def read_schema_revision(connection: Any) -> str | None:
    """Return one revision or None for a truly empty catalog; otherwise reject.

    This is inspection only. Callers must additionally prove that a recorded
    revision belongs to their supported migration graph before any mutation.
    """
    row = connection.execute("""SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=current_schema() AND table_name='alembic_version'
    ) AS present""").fetchone()
    if not _value(row, "present"):
        _require_empty_catalog(connection)
        return None
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if not rows:
        _require_empty_catalog(connection)
        return None
    if len(rows) != 1:
        raise DatabaseSchemaError(
            "The database Alembic revision state is divergent. "
            "Keep the catalog unchanged and use its matching application release."
        )
    revision = _value(rows[0], "version_num")
    if not isinstance(revision, str) or not revision.strip():
        raise DatabaseSchemaError("The database Alembic revision is invalid; mutation is blocked.")
    return revision
