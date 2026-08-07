from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from .config import settings
from .security import hash_password
from .services.rclone_runtime import cleanup_runtime_configs


INTEGRITY_ERRORS = (UniqueViolation, sqlite3.IntegrityError)
HEAD_SCHEMA_REVISION = "0037_directory_aggregates"
_logger = logging.getLogger(__name__)


class DatabaseSchemaError(RuntimeError):
    pass


class SQLiteResult:
    def __init__(self, cursor: sqlite3.Cursor):
        self.cursor = cursor

    @staticmethod
    def _convert(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def fetchone(self) -> dict[str, Any] | None:
        return self._convert(self.cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        """Expose SQLite DML cardinality like psycopg's cursor result."""
        return self.cursor.rowcount


class SQLiteConnection:
    backend = "sqlite"

    def __init__(self, path: str):
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._context_active = False

    def execute(self, sql: str, params: Any = ()) -> SQLiteResult:
        translated = sql.replace("%s", "?")
        return SQLiteResult(self.connection.execute(translated, tuple(params or ())))

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    @property
    def transaction_context_active(self) -> bool:
        """Whether the surrounding ``with SQLiteConnection`` owns commit."""
        return self._context_active

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def begin_immediate(self) -> None:
        """Acquire SQLite's database-wide write lock for an atomic operation."""
        self.connection.execute("BEGIN IMMEDIATE")

    def __enter__(self) -> "SQLiteConnection":
        self._context_active = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self._context_active = False
            self.connection.close()


def db() -> Any:
    if settings.db_backend == "sqlite":
        return SQLiteConnection(settings.sqlite_path)
    return psycopg.connect(row_factory=dict_row, connect_timeout=10)


def _connection_in_transaction(connection: Any) -> bool:
    """Return whether ``connection`` already owns an open transaction.

    The application deliberately passes caller-owned connections into deep
    persistence modules.  A nested helper must not commit or roll back work it
    did not start, so this small adapter keeps SQLite and psycopg semantics in
    one place.
    """
    value = getattr(connection, "in_transaction", None)
    if value is not None:
        return bool(value)
    raw = getattr(connection, "connection", None)
    return bool(getattr(raw, "in_transaction", False))


@contextmanager
def transaction(connection: Any, *, immediate: bool = False) -> Iterator[Any]:
    """Run a caller-compatible transaction without stealing outer ownership.

    ``immediate=True`` takes SQLite's database write lock before the body.  On
    PostgreSQL the first write obtains the equivalent row/table locks selected
    by the caller.  The context commits or rolls back only when it opened the
    transaction itself; callers can therefore compose catalog mutations and
    their revision/event publication atomically.
    """
    owns_transaction = not _connection_in_transaction(connection)
    context_active = bool(getattr(connection, "transaction_context_active", False))
    if context_active:
        # A SQLiteConnection context owns the eventual commit even before its
        # first DML statement starts sqlite3's transaction flag.
        owns_transaction = False
    if not _connection_in_transaction(connection) and immediate:
        begin_immediate = getattr(connection, "begin_immediate", None)
        if begin_immediate is not None:
            begin_immediate()
        elif getattr(connection, "backend", None) == "sqlite":
            connection.execute("BEGIN IMMEDIATE")
    elif (
        not _connection_in_transaction(connection)
        and getattr(connection, "backend", None) == "sqlite"
    ):
        connection.execute("BEGIN")

    try:
        yield connection
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise
    else:
        if owns_transaction:
            connection.commit()


def read_schema_revision(connection: Any) -> str | None:
    """Return the recorded Alembic revision, or None when unversioned."""
    if settings.db_backend == "sqlite":
        version_table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='alembic_version'"
        ).fetchone()
    else:
        version_table = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name='alembic_version'
            ) AS present
            """
        ).fetchone()
        version_table = version_table if version_table["present"] else None
    if not version_table:
        return None
    revision_row = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    return revision_row["version_num"] if revision_row else None


def initialize_database() -> None:
    # Runtime configs are anonymous memfds. Startup only reports retained legacy
    # named residue; it never deletes a pathname after a crash.
    cleanup = cleanup_runtime_configs()
    if any(
        (
            cleanup.skipped_foreign,
            cleanup.skipped_unsafe,
            cleanup.skipped_raced,
        )
    ):
        _logger.warning(
            "Rclone legacy runtime residue retained: untrusted=%d unsafe=%d raced=%d",
            cleanup.skipped_foreign,
            cleanup.skipped_unsafe,
            cleanup.skipped_raced,
        )
    with db() as connection:
        revision = read_schema_revision(connection)
        if revision != HEAD_SCHEMA_REVISION:
            raise DatabaseSchemaError(
                "Database schema is not current. With AUTO_MIGRATE enabled "
                "(default), the container upgrades on start; otherwise run "
                "`python -m app.backup_upgrade` or `alembic upgrade head` "
                f"(found {revision or 'unversioned'}, "
                f"expected {HEAD_SCHEMA_REVISION})."
            )
        _bootstrap_first_admin(connection)


def _bootstrap_first_admin(connection: Any) -> None:
    count = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
    if count:
        return
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        raise RuntimeError(
            "The database has no users: configure BOOTSTRAP_ADMIN_USERNAME and "
            "BOOTSTRAP_ADMIN_PASSWORD for first startup"
        )
    admin = connection.execute(
        """
        INSERT INTO users(username, display_name, password_hash, is_admin)
        VALUES (%s, %s, %s, TRUE)
        RETURNING id
        """,
        (
            settings.bootstrap_admin_username.strip().lower(),
            settings.bootstrap_admin_display_name.strip(),
            hash_password(settings.bootstrap_admin_password),
        ),
    ).fetchone()
    if not settings.bootstrap_vault_slug:
        return
    required: dict[str, Any] = {
        "name": settings.bootstrap_vault_name,
        "source_root": settings.bootstrap_vault_source_root,
        "s3_bucket": settings.bootstrap_vault_s3_bucket,
        "s3_prefix": settings.bootstrap_vault_s3_prefix,
        "rclone_remote": settings.bootstrap_vault_rclone_remote,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Incomplete bootstrap vault configuration: " + ", ".join(missing))
    vault = connection.execute(
        """
        INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix, rclone_remote)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            settings.bootstrap_vault_slug,
            settings.bootstrap_vault_name,
            settings.bootstrap_vault_source_root,
            settings.bootstrap_vault_s3_bucket,
            settings.bootstrap_vault_s3_prefix.strip("/"),
            settings.bootstrap_vault_rclone_remote,
        ),
    ).fetchone()
    connection.execute(
        "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
        (vault["id"], admin["id"]),
    )
