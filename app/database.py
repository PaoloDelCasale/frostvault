from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus

from .config import settings
from .security import hash_password, verify_password


INTEGRITY_ERRORS = (UniqueViolation,)
HEAD_SCHEMA_REVISION = "0069_upload_intent_per_attempt"


class DatabaseSchemaError(RuntimeError):
    pass


def db() -> psycopg.Connection:
    """Connect to PostgreSQL; an empty DSN uses libpq's PG* environment."""
    return psycopg.connect(settings.database_dsn, row_factory=dict_row, connect_timeout=10)


@contextmanager
def transaction(connection: Any) -> Iterator[Any]:
    """Keep commit ownership with non-autocommit callers, even before first SQL.

    psycopg's connection context does not BEGIN until the first statement. Start
    that implicit outer transaction before opening a native savepoint, so a
    later exception in ``with db()`` still rolls back this successful helper.
    Autocommit connections instead get a complete native transaction.
    """
    if not connection.autocommit and connection.info.transaction_status == TransactionStatus.IDLE:
        connection.execute("BEGIN")
    with connection.transaction():
        yield connection


def read_schema_revision(connection: Any) -> str | None:
    """Return the recorded Alembic revision, or None when unversioned."""
    version_table = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_name='alembic_version'
        ) AS present
        """
    ).fetchone()
    if not version_table["present"]:
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
        _bootstrap_owner(connection)


def _bootstrap_owner(connection: Any) -> None:
    """Create the single owner, or apply its env credentials (#412).

    ``BOOTSTRAP_ADMIN_*`` are the owner's local credentials: the emergency
    password accepted only from loopback and the configured LAN CIDRs. They
    are applied at every start, so a restored metadata backup still accepts
    the current password. A changed password revokes existing Sessions.
    """
    owner = connection.execute(
        "SELECT id, username, display_name, password_hash FROM users "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    username = settings.bootstrap_admin_username.strip().lower()
    password = settings.bootstrap_admin_password
    display_name = settings.bootstrap_admin_display_name.strip()
    if owner is None:
        if not username or not password:
            raise RuntimeError(
                "The database has no owner: configure BOOTSTRAP_ADMIN_USERNAME "
                "and BOOTSTRAP_ADMIN_PASSWORD for first startup"
            )
        connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash)
            VALUES (%s, %s, %s)
            """,
            (username, display_name, hash_password(password)),
        )
        return
    if not username or not password:
        return
    password_current = bool(owner["password_hash"]) and verify_password(
        owner["password_hash"], password
    )
    if (
        password_current
        and owner["username"] == username
        and owner["display_name"] == display_name
    ):
        return
    connection.execute(
        """
        UPDATE users
        SET username=%s, display_name=%s, password_hash=%s,
            session_version=session_version + %s
        WHERE id=%s
        """,
        (
            username,
            display_name,
            owner["password_hash"] if password_current else hash_password(password),
            0 if password_current else 1,
            owner["id"],
        ),
    )
