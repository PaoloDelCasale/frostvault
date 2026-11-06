"""DB-only replay policy; integration abort/commit coverage runs on PostgreSQL."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from psycopg.errors import DeadlockDetected, SerializationFailure, UniqueViolation

from app.services.database_retry import (
    DATABASE_RETRY_MAX_ATTEMPTS, is_retryable_transaction_error, retry_database_unit,
)


class DatabaseRetryTests(unittest.TestCase):
    def test_complete_unit_is_replayed_after_unwinding(self) -> None:
        calls = []

        @retry_database_unit
        def operation():
            calls.append("begin")
            try:
                if len(calls) == 1:
                    raise DeadlockDetected("pid 123")
                return "result"
            finally:
                calls.append("closed")

        with patch("app.services.database_retry.time.sleep") as sleep:
            self.assertEqual(operation(), "result")
        self.assertEqual(calls, ["begin", "closed", "begin", "closed"])
        sleep.assert_called_once()

    def test_exhaustion_is_bounded_and_preserves_original_exception(self) -> None:
        error = SerializationFailure("aborted")
        calls = []

        @retry_database_unit
        def operation():
            calls.append(1)
            raise error

        with patch("app.services.database_retry.time.sleep"), self.assertRaises(SerializationFailure) as raised:
            operation()
        self.assertIs(raised.exception, error)
        self.assertEqual(len(calls), DATABASE_RETRY_MAX_ATTEMPTS)

    def test_chain_cycles_are_safe_and_numeric_text_never_retries(self) -> None:
        outer = RuntimeError("wrapper")
        inner = DeadlockDetected("deadlock")
        outer.__cause__ = inner
        inner.__context__ = outer
        self.assertTrue(is_retryable_transaction_error(outer))
        for error in (RuntimeError("deadlock 40P01 pid 500503"), UniqueViolation("40001")):
            self.assertFalse(is_retryable_transaction_error(error))

    def test_batch_marks_coalesce_per_call_not_connection_lifetime(self) -> None:
        from app.services.directory_aggregates import mark_paths_dirty

        class RecordingConnection:
            def __init__(self):
                self.statements = []

            def execute(self, query, params):
                self.statements.append((query, params))

        connection = RecordingConnection()
        paths = [f"nested/{index}.bin" for index in range(100)]
        mark_paths_dirty(connection, 2, paths)
        self.assertEqual(len(connection.statements), 1)
        # A later call must reach the database even with a populated tracker:
        # only PostgreSQL knows whether the transaction changed or rolled back.
        mark_paths_dirty(connection, 2, paths)
        self.assertEqual(len(connection.statements), 2)
        self.assertIn("ON CONFLICT DO NOTHING", connection.statements[0][0])
        self.assertNotIn("DO UPDATE", connection.statements[0][0])

    def test_nontransaction_failures_are_never_replayed(self) -> None:
        for error in (RuntimeError("503"), UniqueViolation("constraint")):
            calls = []

            @retry_database_unit
            def operation():
                calls.append(1)
                raise error

            with self.assertRaises(type(error)):
                operation()
            self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
