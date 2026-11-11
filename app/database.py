from __future__ import annotations

import atexit
import os
import threading
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from . import config
from .config import settings
from .security import hash_password, verify_password


INTEGRITY_ERRORS = (UniqueViolation,)
HEAD_SCHEMA_REVISION = "0069_upload_intent_per_attempt"

CONNECT_TIMEOUT_SECONDS = 10
# Each operation slot may hold two handles at once (a unit opened while an
# outer handle is still open); the API threadpool, watcher, scanner, aggregate
# maintenance, preflight and remediation loops share the headroom (#535).
POOL_HEADROOM = 8
# Bound the pool-specific wait, then use a direct connection rather than fail
# solely because the pool is saturated (nested handles must not deadlock).
POOL_CHECKOUT_TIMEOUT_SECONDS = 1.0
# A burst's surplus connections close one per idle period, down to zero.
POOL_MAX_IDLE_SECONDS = 60.0


class DatabaseSchemaError(RuntimeError):
    pass


def pool_max_size() -> int:
    """Pooled connections kept open at most, from ``OPERATION_CONCURRENCY``."""
    concurrency = getattr(settings, "operation_concurrency", None)
    if concurrency is None:
        concurrency = config.settings.operation_concurrency
    return 2 * max(1, int(concurrency)) + POOL_HEADROOM


def _direct_connection(dsn: str) -> psycopg.Connection:
    """One unpooled connection, closed by its context exit as before #535."""
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SECONDS)


def _reset_session(connection: psycopg.Connection) -> None:
    """Give the next caller the state of a new session.

    Any open transaction is rolled back. ``DISCARD ALL`` then drops session
    settings (back to the startup options), prepared statements, temporary
    tables, session advisory locks and LISTENs. It runs here, in the
    returning thread, so a returned connection is ready before anyone can
    check it out again.
    """
    status = connection.info.transaction_status
    if status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
        connection.rollback()
    elif status != TransactionStatus.IDLE:
        raise psycopg.InterfaceError("connection returned during a statement")
    connection.autocommit = True
    connection.execute("DISCARD ALL")
    connection.autocommit = False
    connection.isolation_level = None
    connection.read_only = None
    connection.deferrable = None
    connection.row_factory = dict_row
    connection.prepare_threshold = None


def _new_pool(dsn: str) -> ConnectionPool:
    return ConnectionPool(
        dsn,
        # psycopg forgets its prepared statements only the first time it sees
        # DISCARD ALL on a connection, so after the next reset it would run
        # statements the server already dropped. Pooled sessions never
        # prepare automatically; the round trip dominates these statements.
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "prepare_threshold": None,
        },
        min_size=0,
        max_size=pool_max_size(),
        open=False,
        # A pooled session can die while idle (server restart, idle timeout);
        # a new connection never could, so check before handing one out.
        check=ConnectionPool.check_connection,
        name="frostvault",
        timeout=POOL_CHECKOUT_TIMEOUT_SECONDS,
        max_idle=POOL_MAX_IDLE_SECONDS,
    )


_pools: dict[str, tuple[int, ConnectionPool]] = {}
_pools_lock = threading.Lock()
# Pools inherited through fork(). They are never closed or collected in the
# child: their sockets belong to the parent's server sessions.
_inherited_pools: list[ConnectionPool] = []


def _forget_pools_after_fork() -> None:
    global _pools_lock
    _pools_lock = threading.Lock()
    _inherited_pools.extend(pool for _, pool in _pools.values())
    _pools.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_pools_after_fork)


def _pool_for(dsn: str) -> ConnectionPool:
    """The process pool for ``dsn``; opening one retires pools of other DSNs.

    Production has a single DSN. Test fixtures switch DSN per database, so
    only the current fixture keeps idle connections open.
    """
    pid = os.getpid()
    retired: list[ConnectionPool] = []
    with _pools_lock:
        current = _pools.get(dsn)
        if current is not None and current[0] == pid:
            return current[1]
        if current is not None:
            _inherited_pools.append(current[1])
        for other in [key for key in _pools if key != dsn]:
            retired.append(_pools.pop(other)[1])
        pool = _new_pool(dsn)
        pool.open()
        _pools[dsn] = (pid, pool)
    for old in retired:
        old.close()
    return pool


def close_pools() -> None:
    """Close every pool; the next ``db()`` opens a new one.

    Connections still checked out stay usable and close when returned.
    """
    with _pools_lock:
        pools = [pool for pid, pool in _pools.values() if pid == os.getpid()]
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


class PooledConnection:
    """One checkout of a pooled connection, used exactly like a new one.

    ``with db() as connection`` yields the psycopg connection, commits or rolls
    back exactly like psycopg's own context exit, then returns it to the pool
    instead of closing it, even when COMMIT raises. ``close()`` returns it once:
    a later call (``owned_connection`` closes after its ``with``) is a no-op,
    so a stale handle can never return a connection another caller now holds.
    Attributes callers set on the connection during the checkout (such as the
    directory-aggregate tracker) are removed, as a new connection would not
    have them.
    """

    __slots__ = ("_pool", "_connection", "_attributes")

    def __init__(self, pool: ConnectionPool, connection: psycopg.Connection) -> None:
        self._pool = pool
        self._connection: psycopg.Connection | None = connection
        self._attributes = frozenset(vars(connection))

    def _checked_out(self) -> psycopg.Connection:
        if self._connection is None:
            raise psycopg.OperationalError("the connection is closed")
        return self._connection

    def __enter__(self) -> psycopg.Connection:
        return self._checked_out()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            # The pool marks the connection as its own, so psycopg commits or
            # rolls back here without closing it.
            connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        try:
            for name in set(vars(connection)) - self._attributes:
                delattr(connection, name)
            if not connection.closed:
                _reset_session(connection)
        except Exception:
            # Unknown session state is never handed out again: the pool
            # replaces a closed connection.
            connection.close()
        finally:
            self._pool.putconn(connection)

    @property
    def closed(self) -> bool:
        return self._connection is None or self._connection.closed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._checked_out(), name)


def db() -> PooledConnection | psycopg.Connection:
    """A PostgreSQL connection; an empty DSN uses libpq's PG* environment.

    Connections come from a process pool (#535) and behave like new ones: use
    them as ``with db() as connection`` (or ``owned_connection(db())``). When
    every pooled connection is in use, or none arrives within
    ``POOL_CHECKOUT_TIMEOUT_SECONDS`` (for example while the server is down),
    the caller gets a direct connection, so errors stay those of a connect.
    """
    dsn = settings.database_dsn
    for _ in range(2):
        pool = _pool_for(dsn)
        stats = pool.get_stats()
        if stats["pool_available"] == 0 and stats["pool_size"] >= pool.max_size:
            return _direct_connection(dsn)
        try:
            return PooledConnection(pool, pool.getconn())
        except PoolClosed:
            # Retired by a concurrent close_pools() or DSN switch: look again.
            continue
        except PoolTimeout:
            return _direct_connection(dsn)
    return _direct_connection(dsn)


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
