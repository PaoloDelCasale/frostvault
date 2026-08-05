from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import backoff
from app.database import SQLiteConnection
from tests.test_database import run_alembic


REPOSITORY_ROOT = Path(__file__).parents[1]


class _LegacyReadBarrierConnection:
    """Force the old read-then-write implementation into its lost-update race."""

    def __init__(self, connection: SQLiteConnection, barrier: threading.Barrier):
        self._connection = connection
        self._barrier = barrier

    def execute(self, sql: str, params=()):
        if sql.lstrip().startswith("SELECT * FROM auth_backoff"):
            self._barrier.wait(timeout=10)
        return self._connection.execute(sql, params)


class BackoffTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "backoff.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

    def connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

    def _fail(self, times: int, *, scope: str = "ip", key: str = "k") -> None:
        with self.connect() as connection:
            for _ in range(times):
                backoff.record_failure(connection, scope=scope, key=key)


class BackoffTests(BackoffTestBase):
    def test_fresh_key_is_allowed(self) -> None:
        with self.connect() as connection:
            backoff.guard(connection, scope="ip", key="k")  # does not raise

    def test_failures_below_threshold_do_not_block(self) -> None:
        self._fail(4)
        with self.connect() as connection:
            backoff.guard(connection, scope="ip", key="k")  # does not raise

    def test_threshold_failures_start_a_thirty_second_backoff(self) -> None:
        self._fail(5)
        with self.connect() as connection:
            with self.assertRaises(backoff.BackoffError) as caught:
                backoff.guard(connection, scope="ip", key="k")
        self.assertGreater(caught.exception.retry_after, 0)
        self.assertLessEqual(caught.exception.retry_after, 30)

    def test_guard_derives_a_deadline_if_an_atomic_write_is_interrupted(self) -> None:
        """A committed increment still throttles if its deadline write never runs."""
        now = backoff._now().isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_backoff(scope, key, failure_count, next_allowed_at, updated_at)
                VALUES (%s, %s, %s, NULL, %s)
                """,
                ("ip", "interrupted", backoff.THRESHOLD, now),
            )
        with self.connect() as connection:
            with self.assertRaises(backoff.BackoffError):
                backoff.guard(connection, scope="ip", key="interrupted")

    def test_backoff_doubles_after_the_threshold(self) -> None:
        self._fail(6)
        with self.connect() as connection:
            with self.assertRaises(backoff.BackoffError) as caught:
                backoff.guard(connection, scope="ip", key="k")
        self.assertGreater(caught.exception.retry_after, 30)
        self.assertLessEqual(caught.exception.retry_after, 60)

    def test_backoff_is_capped_at_fifteen_minutes(self) -> None:
        self._fail(15)
        with self.connect() as connection:
            with self.assertRaises(backoff.BackoffError) as caught:
                backoff.guard(connection, scope="ip", key="k")
        self.assertLessEqual(caught.exception.retry_after, 15 * 60)
        self.assertGreater(caught.exception.retry_after, 15 * 60 - 5)

    def test_success_resets_the_counter(self) -> None:
        self._fail(6)
        with self.connect() as connection:
            backoff.record_success(connection, scope="ip", key="k")
        with self.connect() as connection:
            backoff.guard(connection, scope="ip", key="k")  # does not raise
            self.assertIsNone(
                connection.execute(
                    "SELECT * FROM auth_backoff WHERE scope='ip' AND key='k'"
                ).fetchone()
            )

    def test_counters_decay_after_an_hour_of_quiet(self) -> None:
        self._fail(6)
        with self.connect() as connection:
            connection.execute(
                "UPDATE auth_backoff SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        with self.connect() as connection:
            backoff.guard(connection, scope="ip", key="k")  # decayed: no block
            backoff.record_failure(connection, scope="ip", key="k")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT failure_count FROM auth_backoff WHERE scope='ip' AND key='k'"
            ).fetchone()
        self.assertEqual(row["failure_count"], 1)

    def test_concurrent_sqlite_failures_are_all_counted_atomically(self) -> None:
        """Each connection increments one shared counter without a lost update."""
        attempts = 8
        start = threading.Barrier(attempts, timeout=60)
        legacy_read = threading.Barrier(attempts, timeout=60)

        def record() -> None:
            start.wait()
            with self.connect() as connection:
                backoff.record_failure(
                    _LegacyReadBarrierConnection(connection, legacy_read),
                    scope="ip",
                    key="concurrent",
                )

        with ThreadPoolExecutor(max_workers=attempts) as pool:
            list(pool.map(lambda _: record(), range(attempts)))

        with self.connect() as connection:
            row = connection.execute(
                "SELECT failure_count FROM auth_backoff WHERE scope='ip' AND key='concurrent'"
            ).fetchone()
        self.assertEqual(row["failure_count"], attempts)

    def test_threshold_persists_across_a_process_restart(self) -> None:
        """The durable counter continues throttling after a fresh interpreter starts."""
        self._fail(backoff.THRESHOLD)
        check = """
from app import backoff
from app.database import SQLiteConnection
import sys
with SQLiteConnection(sys.argv[1]) as connection:
    try:
        backoff.guard(connection, scope='ip', key='k')
    except backoff.BackoffError:
        raise SystemExit(0)
raise SystemExit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", check, str(self.database_path)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
