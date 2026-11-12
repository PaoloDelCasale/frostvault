"""Worker errors are deduplicated internal-error problems in the Owner inbox."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from pathlib import Path


from app.services import notifications, worker_errors
from tests.test_database import run_alembic


class WorkerErrorTests(unittest.TestCase):
    def test_worker_failure_is_localized_and_contains_no_exception_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(str(path)) as connection:
                connection.execute("INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')")
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
                notifications.mark_notification_read(connection, notification_id=english[0]["id"], user_id=1)
                self.assertEqual(notifications.count_unread_notifications(connection, user_id=1), 0)
                worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET recurrence"))
                # Reading does not resolve or re-arm one continuously open problem.
                self.assertEqual(notifications.count_unread_notifications(connection, user_id=1), 0)
                self.assertEqual(notifications.count_open_problems(connection, user_id=1), 1)
                from app.services.problems import report_problem
                report_problem(connection, event="internal_error", component="cloud_scan", active=False)
                worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET new episode"))
                self.assertEqual(notifications.count_unread_notifications(connection, user_id=1), 1)
                self.assertEqual(len(notifications.list_in_app_notifications(connection, user_id=1)), 1)

    def test_fresh_database_before_owner_bootstrap_has_no_recipient(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(str(path)) as connection:
                self.assertIsNone(worker_errors.record_worker_error(connection, component="startup", exc=RuntimeError("not exposed")))
