from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from .config import settings
from .security import hash_password


INTEGRITY_ERRORS = (UniqueViolation, sqlite3.IntegrityError)
HEAD_SCHEMA_REVISION = "0029_source_volume_identity"


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


class SQLiteConnection:
    backend = "sqlite"

    def __init__(self, path: str):
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")

    def execute(self, sql: str, params: Any = ()) -> SQLiteResult:
        translated = sql.replace("%s", "?")
        return SQLiteResult(self.connection.execute(translated, tuple(params or ())))

    def commit(self) -> None:
        self.connection.commit()

    def begin_immediate(self) -> None:
        """Acquire SQLite's database-wide write lock for an atomic operation."""
        self.connection.execute("BEGIN IMMEDIATE")

    def __enter__(self) -> "SQLiteConnection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()


def db() -> Any:
    if settings.db_backend == "sqlite":
        return SQLiteConnection(settings.sqlite_path)
    return psycopg.connect(row_factory=dict_row, connect_timeout=10)


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
