from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import backoff
from app.database import SQLiteConnection
from tests.test_database import run_alembic


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


if __name__ == "__main__":
    unittest.main()
