"""Classified persisted background-worker errors (issue #16).

Seams under test:
- ``record_worker_error`` / ``list_worker_errors``
- background loop records classified errors instead of swallowing silently
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from app.services import worker_errors
from tests.test_database import run_alembic


class WorkerErrorPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "worker.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

    def test_recorded_worker_error_is_classified_and_retrievable(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            recorded = worker_errors.record_worker_error(
                connection,
                component="background_loop",
                exc=TimeoutError("S3 list timed out"),
                vault_id=None,
            )
            listed = worker_errors.list_worker_errors(connection)

        self.assertEqual(recorded["component"], "background_loop")
        self.assertEqual(recorded["classification"], "timeout")
        self.assertIn("timed out", recorded["message"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], recorded["id"])

    def test_classify_maps_common_failure_kinds(self) -> None:
        self.assertEqual(
            worker_errors.classify_exception(ConnectionError("db down")),
            "connectivity",
        )
        self.assertEqual(
            worker_errors.classify_exception(PermissionError("denied")),
            "permission",
        )
        self.assertEqual(
            worker_errors.classify_exception(ValueError("bad config")),
            "configuration",
        )
        self.assertEqual(
            worker_errors.classify_exception(RuntimeError("boom")),
            "unexpected",
        )


if __name__ == "__main__":
    unittest.main()
