"""Pooled ``db()`` connections behave like new ones (#535)."""
from __future__ import annotations

import os
import select
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg_pool import PoolTimeout

from app import config, database
from app.services.database_retry import owned_connection
from tests.postgres import PostgresConnection, database_dsn


class _Connection:
    """Stands in for a psycopg connection: plain attributes, a context exit."""

    closed = False

    def __init__(self, exit_error: BaseException | None = None):
        self.exits: list[type[BaseException] | None] = []
        self.exit_error = exit_error

    def __exit__(self, exc_type, exc_value, traceback):
        self.exits.append(exc_type)
        if self.exit_error is not None:
            raise self.exit_error

    def close(self):
        self.closed = True


class _Pool:
    max_size = 2

    def __init__(self, *, size=0, available=0, error: BaseException | None = None):
        self.size, self.available, self.error = size, available, error
        self.returned: list[object] = []

    def get_stats(self):
        return {"pool_size": self.size, "pool_available": self.available}

    def getconn(self):
        if self.error is not None:
            raise self.error
        return _Connection()

    def putconn(self, connection):
        self.returned.append(connection)


class PooledConnectionTests(unittest.TestCase):
    def setUp(self):
        self.reset = self.enterContext(patch.object(database, "_reset_session"))

    def test_pool_size_follows_operation_concurrency(self):
        with patch.object(database, "settings", SimpleNamespace(database_dsn="", operation_concurrency=16)):
            self.assertEqual(database.pool_max_size(), 2 * 16 + database.POOL_HEADROOM)
        with patch.object(database, "settings", SimpleNamespace(database_dsn="")):
            self.assertEqual(
                database.pool_max_size(),
                2 * config.settings.operation_concurrency + database.POOL_HEADROOM,
            )

    def test_a_checkout_returns_its_connection_exactly_once(self):
        pool, connection = _Pool(), _Connection()
        lease = database.PooledConnection(pool, connection)
        with owned_connection(lease) as active:
            self.assertIs(active, connection)
        lease.close()
        self.assertEqual(pool.returned, [connection])
        self.assertEqual(connection.exits, [None])
        with self.assertRaises(psycopg.OperationalError):
            lease.execute("SELECT 1")

    def test_a_failing_commit_still_returns_the_connection(self):
        pool = _Pool()
        connection = _Connection(exit_error=psycopg.errors.SerializationFailure("commit"))
        with self.assertRaises(psycopg.errors.SerializationFailure):
            with owned_connection(database.PooledConnection(pool, connection)):
                pass
        self.assertEqual(pool.returned, [connection])

    def test_a_session_that_cannot_be_reset_is_closed_before_its_return(self):
        pool, connection = _Pool(), _Connection()
        self.reset.side_effect = psycopg.OperationalError("server closed the connection")
        with database.PooledConnection(pool, connection):
            pass
        self.assertTrue(connection.closed)
        self.assertEqual(pool.returned, [connection])

    def test_attributes_set_during_a_checkout_do_not_reach_the_next_one(self):
        pool, connection = _Pool(), _Connection()
        connection.kept = "from the driver"
        with database.PooledConnection(pool, connection) as active:
            active._directory_aggregate_tracker = object()
        self.assertFalse(hasattr(connection, "_directory_aggregate_tracker"))
        self.assertEqual(connection.kept, "from the driver")

    def test_a_saturated_or_unavailable_pool_falls_back_to_a_direct_connection(self):
        pools = {
            "saturated": _Pool(size=_Pool.max_size, available=0),
            "timeout": _Pool(error=PoolTimeout("no connection")),
        }
        for name, pool in pools.items():
            with self.subTest(name), \
                    patch.object(database, "settings", SimpleNamespace(database_dsn="")), \
                    patch.object(database, "_pool_for", return_value=pool), \
                    patch.object(database.psycopg, "connect") as connect:
                self.assertIs(database.db(), connect.return_value)
            self.assertEqual(connect.call_args.args, ("",))
            self.assertEqual(connect.call_args.kwargs["row_factory"], database.dict_row)

    def test_a_free_pooled_connection_is_used(self):
        pool = _Pool(size=1, available=1)
        with patch.object(database, "settings", SimpleNamespace(database_dsn="")), \
                patch.object(database, "_pool_for", return_value=pool), \
                patch.object(database.psycopg, "connect") as connect:
            lease = database.db()
        self.assertIsInstance(lease, database.PooledConnection)
        connect.assert_not_called()


class PooledDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pool"
        self.dsn = database_dsn(self.path)
        self.addCleanup(database.close_pools)
        self.enterContext(patch.object(database, "settings", SimpleNamespace(database_dsn=self.dsn)))
        with PostgresConnection(self.path) as connection:
            connection.execute("CREATE TABLE notes(id int PRIMARY KEY)")

    def test_the_next_checkout_reuses_the_session_with_a_fresh_state(self):
        with database.db() as connection:
            pid = connection.info.backend_pid
            startup_lock_timeout = connection.execute("SHOW lock_timeout").fetchone()["lock_timeout"]
            connection.execute("SET lock_timeout='1s'")
            connection.execute("SELECT pg_advisory_lock(535)")
            connection.execute("CREATE TEMP TABLE scratch(id int)")
            connection.execute("PREPARE probe AS SELECT 1")
            connection.commit()
            connection.autocommit = True
            connection.read_only = True
        with database.db() as connection:
            self.assertEqual(connection.info.backend_pid, pid)
            self.assertFalse(connection.autocommit)
            self.assertIsNone(connection.read_only)
            row = connection.execute(
                """
                SELECT current_setting('lock_timeout') AS lock_timeout,
                       (SELECT COUNT(*) FROM pg_locks WHERE locktype='advisory'
                          AND pid=pg_backend_pid()) AS advisory,
                       to_regclass('pg_temp.scratch') AS scratch,
                       (SELECT COUNT(*) FROM pg_prepared_statements
                          WHERE name='probe') AS prepared
                """
            ).fetchone()
        self.assertEqual(row, {"lock_timeout": startup_lock_timeout, "advisory": 0,
                               "scratch": None, "prepared": 0})

    def test_statements_repeated_across_checkouts_survive_each_reset(self):
        # psycopg prepares a statement after five runs. It forgets its
        # prepared statements on DISCARD ALL only while that command is new to
        # the connection: a reset before any statement was prepared would
        # leave later checkouts naming statements the server dropped.
        pids = set()
        with database.db() as connection:
            pids.add(connection.info.backend_pid)
            connection.execute("SELECT 1")
        for value in range(3):
            with database.db() as connection:
                pids.add(connection.info.backend_pid)
                for _ in range(8):
                    connection.execute("INSERT INTO notes VALUES (%s) ON CONFLICT DO NOTHING", (value,))
        self.assertEqual(len(pids), 1)
        with PostgresConnection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"], 3)

    def test_an_error_rolls_back_and_an_unclosed_transaction_is_discarded(self):
        with self.assertRaises(RuntimeError):
            with database.db() as connection:
                connection.execute("INSERT INTO notes VALUES (1)")
                raise RuntimeError("abort")
        lease = database.db()
        lease.execute("INSERT INTO notes VALUES (2)")
        lease.close()
        with database.db() as connection:
            connection.execute("INSERT INTO notes VALUES (3)")
        with PostgresConnection(self.path) as connection:
            rows = connection.execute("SELECT id FROM notes ORDER BY id").fetchall()
        self.assertEqual([row["id"] for row in rows], [3])

    def test_switching_database_closes_the_previous_pool(self):
        with database.db() as connection:
            connection.execute("SELECT 1")
        previous = database._pool_for(self.dsn)
        other = Path(self.tmp.name) / "other"
        with patch.object(database, "settings", SimpleNamespace(database_dsn=database_dsn(other))):
            with database.db() as connection:
                self.assertEqual(connection.execute("SELECT current_database() AS name").fetchone()["name"],
                                 psycopg.conninfo.conninfo_to_dict(database_dsn(other))["dbname"])
        self.assertTrue(previous.closed)

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable on this platform")
    def test_forked_child_uses_its_own_pool_and_leaves_parent_session_usable(self):
        with database.db() as connection:
            parent_backend = connection.info.backend_pid
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            try:
                with database.db() as connection:
                    os.write(writer, str(connection.info.backend_pid).encode("ascii"))
                database.close_pools()
            except BaseException:
                os._exit(1)
            os._exit(0)
        os.close(writer)
        try:
            ready, _, _ = select.select([reader], [], [], 10)
            if not ready:
                os.kill(child, signal.SIGKILL)
            _, status = os.waitpid(child, 0)
            self.assertTrue(ready, "child could not acquire its own pool after fork")
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertNotEqual(int(os.read(reader, 32)), parent_backend)
        finally:
            os.close(reader)
        with database.db() as connection:
            self.assertEqual(connection.info.backend_pid, parent_backend)
            self.assertEqual(connection.execute("SELECT 1 AS n").fetchone()["n"], 1)

    def test_close_pools_closes_idle_sessions(self):
        with database.db() as connection:
            pid = connection.info.backend_pid
        database.close_pools()
        with PostgresConnection(self.path) as connection:
            alive = connection.execute("SELECT COUNT(*) AS n FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()["n"]
        self.assertEqual(alive, 0)


if __name__ == "__main__":
    unittest.main()
