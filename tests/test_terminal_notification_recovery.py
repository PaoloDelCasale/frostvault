"""Recover terminal notifications after enqueue faults (issue #300)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services import notifications as notification_service
from app.storage import set_job
from tests.test_database import run_alembic


class TerminalNotificationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "notify.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin, active) "
                "VALUES (1, 'owner', 'Owner', 'hash', TRUE, TRUE)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')"
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (2, 1, 'owner')"
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="a.txt",
                file_type="regular",
                size=1,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            self.job_id = int(
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at
                    )
                    SELECT 2, id, 'a.txt', 'upload', 'uploading', 1,
                           '2026-07-21T10:00:00+00:00', '2026-07-21T10:00:00+00:00'
                    FROM vault_files WHERE vault_id=2 LIMIT 1
                    RETURNING id
                    """
                ).fetchone()["id"]
            )

    def test_terminal_notification_insert_failure_is_retried(self) -> None:
        with patch(
            "app.storage.db",
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ), patch(
            "app.services.notifications.enqueue_job_terminal_push",
            side_effect=RuntimeError("savepoint boom"),
        ):
            self.assertTrue(set_job(self.job_id, "failed", "AccessDenied"))

        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, terminal_notification_status FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            notifications = connection.execute(
                "SELECT id FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchall()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["terminal_notification_status"], "pending")
        self.assertEqual(notifications, [])

        with SQLiteConnection(str(self.path)) as connection:
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
                "SELECT terminal_notification_status FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()["terminal_notification_status"]
        self.assertEqual(recovered, 1)
        self.assertEqual(backlog, 0)
        self.assertEqual(status, "done")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "job_failed")

        with SQLiteConnection(str(self.path)) as connection:
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
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ):
            self.assertTrue(set_job(self.job_id, "completed", "ok"))
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, terminal_notification_status FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT event FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchall()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["terminal_notification_status"], "done")
        self.assertEqual([row["event"] for row in rows], ["job_completed"])


if __name__ == "__main__":
    unittest.main()
