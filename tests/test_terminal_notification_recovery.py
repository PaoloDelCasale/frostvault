"""Recover terminal notifications after enqueue faults (issue #300)."""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

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
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')"
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
                        requested_at, updated_at
                    )
                    SELECT 2, id, 'a.txt', 'upload', 'uploading',
                           '2026-07-21T10:00:00+00:00', '2026-07-21T10:00:00+00:00'
                    FROM vault_files WHERE vault_id=2 LIMIT 1
                    RETURNING id
                    """
                ).fetchone()["id"]
            )

    def test_terminal_notification_insert_failure_is_retried(self) -> None:
        with patch(
            "app.storage.db",
            side_effect=lambda: PostgresConnection(str(self.path)),
        ), patch(
            "app.services.notifications.enqueue_job_terminal_notification",
            side_effect=RuntimeError("savepoint boom"),
        ):
            self.assertTrue(set_job(self.job_id, "failed", "AccessDenied"))

        with PostgresConnection(str(self.path)) as connection:
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

        with PostgresConnection(str(self.path)) as connection:
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

        with PostgresConnection(str(self.path)) as connection:
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
            side_effect=lambda: PostgresConnection(str(self.path)),
        ):
            self.assertTrue(set_job(self.job_id, "completed", "ok"))
        with PostgresConnection(str(self.path)) as connection:
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
        self.assertEqual(rows, [])  # Routine upload success is intentionally silent.


if __name__ == "__main__":
    unittest.main()
