"""Worker errors are deduplicated internal-error problems in the Owner inbox."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from app.services import notifications, worker_errors
from tests.test_database import run_alembic


class WorkerErrorTests(unittest.TestCase):
    def test_worker_failure_is_localized_and_contains_no_exception_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1, 'owner', 'Owner', 'hash')")
                for _ in range(2):
                    worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET raw exception"), password="SECRET")
                english = notifications.list_in_app_notifications(connection, user_id=1, locale="en")
                italian = notifications.list_in_app_notifications(connection, user_id=1, locale="it")
                self.assertEqual(len(english), 1)
                self.assertEqual(english[0]["event"], "internal_error")
                self.assertIn("internal error", english[0]["title"])
                self.assertIn("errore interno", italian[0]["title"])
                self.assertNotIn("SECRET", str(english))
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM notification_deliveries").fetchone()["n"], 0)

    def test_fresh_database_before_owner_bootstrap_has_no_recipient(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with SQLiteConnection(str(path)) as connection:
                self.assertIsNone(worker_errors.record_worker_error(connection, component="startup", exc=RuntimeError("not exposed")))
