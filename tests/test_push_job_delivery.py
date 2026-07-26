"""Job completion Web Push delivery and security seams (issue #72, seams 5–7)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.services import notifications
from app.sessions import create_session, revoke_session
from tests.test_database import run_alembic


class _RecordingPush:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, *, subscription: dict, payload: dict) -> None:
        self.messages.append({"subscription": subscription, "payload": payload})


class JobPushDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "push.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.test_settings = replace(
            settings,
            vapid_public_key="BP-test-public-key-not-a-placeholder",
            vapid_private_key="test-private-key-not-a-placeholder",
            vapid_subject="mailto:ops@example.com",
        )
        self._patcher = patch("app.services.notifications.settings", self.test_settings)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self._cfg_patcher = patch(
            "app.services.notifications.push_configured",
            lambda settings_obj=None: True,
        )
        self._cfg_patcher.start()
        self.addCleanup(self._cfg_patcher.stop)

        with SQLiteConnection(str(self.path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE) RETURNING id
                """,
                (hash_password("owner-password-1"),),
            ).fetchone()["id"]
            self.outsider_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('outsider', 'Outsider', %s, FALSE) RETURNING id
                """,
                (hash_password("outsider-password-1"),),
            ).fetchone()["id"]
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )
            vault_file = connection.execute(
                """
                INSERT INTO vault_files(id, vault_id, status, created_at)
                VALUES ('22222222-2222-2222-2222-222222222222', %s, 'active',
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
                    %s, %s, 'a.txt', 'recover', 'queued', %s,
                    '2026-07-22T00:00:00+00:00', '2026-07-22T00:00:00+00:00'
                )
                """,
                (self.vault_id, vault_file["id"], self.owner_id),
            )
            self.job_id = connection.execute(
                "SELECT id FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

            owner_token = create_session(
                connection, user_id=self.owner_id, auth_method="local"
            )
            self.owner_session_id = connection.execute(
                "SELECT id FROM sessions WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.owner_id,),
            ).fetchone()["id"]
            self.owner_token = owner_token

            outsider_token = create_session(
                connection, user_id=self.outsider_id, auth_method="local"
            )
            self.outsider_session_id = connection.execute(
                "SELECT id FROM sessions WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.outsider_id,),
            ).fetchone()["id"]
            del outsider_token

            notifications.upsert_push_subscription(
                connection,
                user_id=self.owner_id,
                session_id=self.owner_session_id,
                endpoint="https://push.example/owner",
                p256dh="owner-p256dh",
                auth="owner-auth",
            )
            notifications.upsert_push_subscription(
                connection,
                user_id=self.outsider_id,
                session_id=self.outsider_session_id,
                endpoint="https://push.example/outsider",
                p256dh="outsider-p256dh",
                auth="outsider-auth",
            )

    def test_completed_job_delivers_only_to_vault_members(self) -> None:
        push = _RecordingPush()
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s",
                (self.job_id,),
            )
            enqueued = notifications.enqueue_job_terminal_push(
                connection, job_id=self.job_id
            )
            self.assertEqual(enqueued, 1)
            notifications.deliver_pending_notifications(
                connection, push_client=push, max_attempts=1
            )

        self.assertEqual(len(push.messages), 1)
        self.assertEqual(
            push.messages[0]["subscription"]["endpoint"],
            "https://push.example/owner",
        )
        self.assertEqual(push.messages[0]["payload"]["title"], "Job completed")

    def test_revoked_session_stops_push_delivery(self) -> None:
        push = _RecordingPush()
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s",
                (self.job_id,),
            )
            notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                job_id=self.job_id,
                event="job_completed",
                title="Job completed",
                body="recover: a.txt",
                channels=("push",),
            )
            revoke_session(connection, self.owner_session_id)
            remaining = connection.execute(
                "SELECT COUNT(*) AS c FROM push_subscriptions WHERE session_id=%s",
                (self.owner_session_id,),
            ).fetchone()["c"]
            self.assertEqual(remaining, 0)

            notifications.deliver_pending_notifications(
                connection, push_client=push, max_attempts=1
            )

        self.assertEqual(push.messages, [])

    def test_removed_membership_stops_push_delivery(self) -> None:
        push = _RecordingPush()
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='failed' WHERE id=%s",
                (self.job_id,),
            )
            notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                job_id=self.job_id,
                event="job_failed",
                title="Job failed",
                body="recover: a.txt",
                channels=("push",),
            )
            connection.execute(
                "DELETE FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.owner_id),
            )
            notifications.deliver_pending_notifications(
                connection, push_client=push, max_attempts=1
            )
        self.assertEqual(push.messages, [])

    def test_unconfigured_push_enqueues_nothing(self) -> None:
        with patch(
            "app.services.notifications.push_configured",
            lambda settings_obj=None: False,
        ):
            with SQLiteConnection(str(self.path)) as connection:
                connection.execute(
                    "UPDATE jobs SET status='completed' WHERE id=%s",
                    (self.job_id,),
                )
                enqueued = notifications.enqueue_job_terminal_push(
                    connection, job_id=self.job_id
                )
        self.assertEqual(enqueued, 0)


if __name__ == "__main__":
    unittest.main()
