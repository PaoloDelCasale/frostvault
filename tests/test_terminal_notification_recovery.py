"""Recover terminal notifications after enqueue faults (issue #300)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.services import notifications as notification_service
from app.storage import set_job
from tests.fixtures import FrostVaultTestCase, seed_job, seed_vault


# Ending a Job queues its outcome in ``job_terminal_notifications`` (#589): a
# row there is a pending notification, its absence a materialized one.
JOB_NOTIFICATION_SQL = """
    SELECT j.status,
           CASE WHEN o.job_id IS NULL THEN 'done' ELSE 'pending' END
               AS terminal_notification_status
    FROM jobs j LEFT JOIN job_terminal_notifications o ON o.job_id = j.id
    WHERE j.id=%s
"""


class TerminalNotificationRecoveryTests(FrostVaultTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=2)
            self.job_id = seed_job(connection, vault_id=2, status="uploading")
        self.enterContext(patch(
            "app.services.upload_failure_problems.CLOSED_UPLOAD_FAILURE_SQL", "FALSE"))

    def test_terminal_notification_insert_failure_is_retried(self) -> None:
        with patch(
            "app.storage.db",
            side_effect=lambda: self.database.connect(),
        ), patch(
            "app.services.notifications.enqueue_job_terminal_notification",
            side_effect=RuntimeError("savepoint boom"),
        ):
            self.assertTrue(set_job(self.job_id, "failed", "AccessDenied"))

        with self.database.connect() as connection:
            job = connection.execute(
                JOB_NOTIFICATION_SQL,
                (self.job_id,),
            ).fetchone()
            notifications = connection.execute(
                "SELECT id FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchall()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["terminal_notification_status"], "pending")
        self.assertEqual(notifications, [])

        with self.database.connect() as connection:
            recovered = notification_service.reconcile_pending_terminal_notifications(
                connection
            )
            backlog = notification_service.pending_terminal_notification_count(
                connection
            )
            rows = connection.execute(
                "SELECT event, dedupe_key FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchall()
            status = connection.execute(
                JOB_NOTIFICATION_SQL,
                (self.job_id,),
            ).fetchone()["terminal_notification_status"]
        self.assertEqual(recovered, 1)
        self.assertEqual(backlog, 0)
        self.assertEqual(status, "done")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "job_failed")

        with self.database.connect() as connection:
            again = notification_service.reconcile_pending_terminal_notifications(
                connection
            )
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchone()["n"]
        self.assertEqual(again, 0)
        self.assertEqual(count, 1)

    def test_successful_terminal_transition_marks_notification_done(self) -> None:
        with patch(
            "app.storage.db",
            side_effect=lambda: self.database.connect(),
        ):
            self.assertTrue(set_job(self.job_id, "completed", "ok"))
        with self.database.connect() as connection:
            job = connection.execute(
                JOB_NOTIFICATION_SQL,
                (self.job_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT event FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchall()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["terminal_notification_status"], "done")
        self.assertEqual(rows, [])  # Routine upload success is intentionally silent.


if __name__ == "__main__":
    unittest.main()
