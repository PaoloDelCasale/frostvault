"""Worker errors are deduplicated internal-error problems in the Owner inbox."""
from __future__ import annotations

import unittest

from app.services import notifications, worker_errors
from tests.fixtures import create_owner, fresh_database


class WorkerErrorTests(unittest.TestCase):
    def test_worker_failure_is_localized_and_contains_no_exception_payload(self):
        with fresh_database() as database:
            with database.connect() as connection:
                create_owner(connection)
                for _ in range(2):
                    worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET raw exception"), password="SECRET")
                english = notifications.list_in_app_notifications(connection, locale="en")
                italian = notifications.list_in_app_notifications(connection, locale="it")
                self.assertEqual(len(english), 1)
                self.assertEqual(english[0]["event"], "internal_error")
                self.assertIn("internal error", english[0]["title"])
                self.assertIn("errore interno", italian[0]["title"])
                self.assertNotIn("SECRET", str(english))
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM notification_deliveries").fetchone()["n"], 0)
                notifications.mark_notification_read(connection, notification_id=english[0]["id"])
                self.assertEqual(notifications.count_unread_notifications(connection), 0)
                worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET recurrence"))
                # Reading does not resolve or re-arm one continuously open problem.
                self.assertEqual(notifications.count_unread_notifications(connection), 0)
                self.assertEqual(notifications.count_open_problems(connection), 1)
                from app.services.problems import report_problem
                report_problem(connection, event="internal_error", component="cloud_scan", active=False)
                worker_errors.record_worker_error(connection, component="cloud_scan", exc=RuntimeError("SECRET new episode"))
                self.assertEqual(notifications.count_unread_notifications(connection), 1)
                self.assertEqual(len(notifications.list_in_app_notifications(connection)), 1)

    def test_fresh_database_before_owner_bootstrap_keeps_the_problem_for_the_owner(self):
        # #586: the inbox is the single Owner's, not a per-user recipient list.
        # A failure before bootstrap is still recorded once, without payload.
        with fresh_database() as database:
            with database.connect() as connection:
                for _ in range(2):
                    worker_errors.record_worker_error(connection, component="startup", exc=RuntimeError("not exposed"))
                rows = notifications.list_in_app_notifications(connection)
                self.assertEqual([row["event"] for row in rows], ["internal_error"])
                self.assertNotIn("not exposed", str(rows))
