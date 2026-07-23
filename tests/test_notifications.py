"""In-app notifications and bounded delivery retries (issue #16).

Seams under test:
- ``enqueue_notification`` / ``list_in_app_notifications``
- ``deliver_pending_notifications`` with injectable webhook/SMTP adapters
- Delivery retries are bounded and never mutate the underlying job status
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from app.security import hash_password
from app.services import notifications
from tests.test_database import run_alembic


class _FailingWebhook:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, payload: dict) -> None:
        self.calls += 1
        raise RuntimeError("webhook down")


class _RecordingSmtp:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, message: dict) -> None:
        self.messages.append(message)


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "notify.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE)
                """,
                (hash_password("owner-password-1"),),
            )
            self.user_id = connection.execute(
                "SELECT id FROM users WHERE username='owner'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs', 'remote')
                """
            )
            self.vault_id = connection.execute(
                "SELECT id FROM vaults WHERE slug='docs'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.user_id),
            )
            vault_file = connection.execute(
                """
                INSERT INTO vault_files(id, vault_id, status, created_at)
                VALUES ('11111111-1111-1111-1111-111111111111', %s, 'active',
                        '2026-07-22T00:00:00+00:00')
                RETURNING id
                """,
                (self.vault_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status, requested_by,
                    requested_at, updated_at
                ) VALUES (
                    %s, %s, 'a.txt', 'upload', 'completed', %s,
                    '2026-07-22T00:00:00+00:00', '2026-07-22T00:00:00+00:00'
                )
                """,
                (self.vault_id, vault_file["id"], self.user_id),
            )
            self.job_id = connection.execute(
                "SELECT id FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    def test_enqueue_creates_retrievable_in_app_notification(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="vault_membership_changed",
                title="Membership changed",
                body="A viewer was added",
                job_id=self.job_id,
            )
            listed = notifications.list_in_app_notifications(
                connection, user_id=self.user_id
            )
        self.assertEqual(item["event"], "vault_membership_changed")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "Membership changed")
        self.assertFalse(listed[0]["read"])

    def test_webhook_retries_are_bounded_and_do_not_change_job_result(self) -> None:
        webhook = _FailingWebhook()
        smtp = _RecordingSmtp()
        with SQLiteConnection(str(self.path)) as connection:
            notifications.set_vault_notification_preference(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                channel="webhook",
                enabled=True,
            )
            notifications.set_global_webhook_endpoint(
                connection,
                url="https://hooks.example/archive",
                enabled=True,
            )
            notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
                job_id=self.job_id,
                channels=("in_app", "webhook"),
            )
            # Four delivery passes with max_attempts=3 must stop retrying.
            for _ in range(4):
                notifications.deliver_pending_notifications(
                    connection,
                    webhook_client=webhook,
                    smtp_client=smtp,
                    max_attempts=3,
                )
            job = connection.execute(
                "SELECT status, message FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            deliveries = connection.execute(
                """
                SELECT status, attempt_count FROM notification_deliveries
                WHERE channel='webhook'
                """
            ).fetchall()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["status"], "failed")
        self.assertEqual(deliveries[0]["attempt_count"], 3)
        self.assertEqual(webhook.calls, 3)
        self.assertEqual(smtp.messages, [])


if __name__ == "__main__":
    unittest.main()
