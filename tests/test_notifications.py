"""In-app notifications and bounded delivery retries (issue #16).

Seams under test:
- ``enqueue_notification`` / ``list_in_app_notifications``
- ``deliver_pending_notifications`` with injectable webhook/SMTP adapters
- Delivery retries are bounded and never mutate the underlying job status
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.security import hash_password
from app.services import notifications
from app.sessions import create_session, csrf_token_for
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
            self.outsider_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('outsider', 'Outsider', %s, FALSE)
                RETURNING id
                """,
                (hash_password("outsider-password-1"),),
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

    def test_personal_preferences_are_strict_and_isolated(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            saved = notifications.set_user_vault_notification_preference(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="job_completed",
                channel="in_app",
                enabled=False,
            )
            self.assertEqual(saved["user_id"], self.user_id)
            self.assertFalse(saved["enabled"])
            self.assertEqual(
                notifications.list_user_vault_notification_preferences(
                    connection, user_id=self.user_id, vault_id=self.vault_id
                ),
                [saved],
            )
            self.assertEqual(
                notifications.list_user_vault_notification_preferences(
                    connection, user_id=self.outsider_id, vault_id=self.vault_id
                ),
                [],
            )
            other_vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('other', 'Other', '/other', 'bucket', 'other', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (other_vault_id, self.user_id),
            )
            other_saved = notifications.set_user_vault_notification_preference(
                connection,
                user_id=self.user_id,
                vault_id=other_vault_id,
                event="job_failed",
                channel="push",
                enabled=True,
            )
            self.assertEqual(
                notifications.list_user_vault_notification_preferences(
                    connection, user_id=self.user_id, vault_id=self.vault_id
                ),
                [saved],
            )
            self.assertEqual(
                notifications.list_user_vault_notification_preferences(
                    connection, user_id=self.user_id, vault_id=other_vault_id
                ),
                [other_saved],
            )
            with self.assertRaisesRegex(ValueError, "event"):
                notifications.set_user_vault_notification_preference(
                    connection,
                    user_id=self.user_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    channel="in_app",
                )
            with self.assertRaisesRegex(ValueError, "channel"):
                notifications.set_user_vault_notification_preference(
                    connection,
                    user_id=self.user_id,
                    vault_id=self.vault_id,
                    event="job_completed",
                    channel="webhook",
                )
            with self.assertRaises(ValueError):
                notifications.set_user_vault_notification_preference(
                    connection,
                    user_id=self.outsider_id,
                    vault_id=self.vault_id,
                    event="job_failed",
                    channel="in_app",
                )

    def test_terminal_in_app_defaults_enabled_without_push_configuration(self) -> None:
        with patch(
            "app.services.notifications.push_configured",
            lambda settings_obj=None: False,
        ):
            with SQLiteConnection(str(self.path)) as connection:
                for status, event in (
                    ("completed", "job_completed"),
                    ("failed", "job_failed"),
                ):
                    connection.execute(
                        "UPDATE jobs SET status=%s WHERE id=%s",
                        (status, self.job_id),
                    )
                    enqueued = notifications.enqueue_job_terminal_push(
                        connection, job_id=self.job_id
                    )
                    row = connection.execute(
                        "SELECT in_app_enabled, dedupe_key FROM notifications "
                        "WHERE user_id=%s AND job_id=%s AND event=%s",
                        (self.user_id, self.job_id, event),
                    ).fetchone()
                    self.assertEqual(enqueued, 1)
                    self.assertTrue(row["in_app_enabled"])
                    self.assertEqual(
                        row["dedupe_key"], f"job:{self.job_id}:{event}"
                    )
                listed = notifications.list_in_app_notifications(
                    connection, user_id=self.user_id
                )
        self.assertEqual(len(listed), 2)

    def test_terminal_in_app_opt_out_suppresses_inbox_row(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            notifications.set_user_vault_notification_preference(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="job_completed",
                channel="in_app",
                enabled=False,
            )
            with patch(
                "app.services.notifications.push_configured",
                lambda settings_obj=None: False,
            ):
                enqueued = notifications.enqueue_job_terminal_push(
                    connection, job_id=self.job_id
                )
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM notifications WHERE job_id=%s",
                (self.job_id,),
            ).fetchone()["total"]
        self.assertEqual(enqueued, 0)
        self.assertEqual(count, 0)

    def test_unread_count_ignores_limit_and_push_only_rows(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            for index in range(3):
                notifications.enqueue_notification(
                    connection,
                    user_id=self.user_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Upload {index}",
                )
            notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="job_completed",
                title="Push only",
                channels=("push",),
            )
            listed = notifications.list_in_app_notifications(
                connection, user_id=self.user_id, limit=1
            )
            unread_count = notifications.count_unread_notifications(
                connection, user_id=self.user_id
            )
        self.assertEqual(len(listed), 1)
        self.assertEqual(unread_count, 3)
        self.assertNotIn("Push only", {item["title"] for item in listed})

    def test_terminal_notification_deduplicates_canonical_row_and_delivery(self) -> None:
        with patch(
            "app.services.notifications.push_configured",
            lambda settings_obj=None: False,
        ):
            with SQLiteConnection(str(self.path)) as connection:
                first = notifications.enqueue_job_terminal_push(
                    connection, job_id=self.job_id
                )
                second = notifications.enqueue_job_terminal_push(
                    connection, job_id=self.job_id
                )
                rows = connection.execute(
                    "SELECT id, dedupe_key FROM notifications WHERE job_id=%s",
                    (self.job_id,),
                ).fetchall()
                deliveries = connection.execute(
                    "SELECT COUNT(*) AS total FROM notification_deliveries "
                    "WHERE notification_id IN "
                    "(SELECT id FROM notifications WHERE job_id=%s)",
                    (self.job_id,),
                ).fetchone()["total"]
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], f"job:{self.job_id}:job_completed")
        self.assertEqual(deliveries, 0)

    def test_keyed_terminal_notification_renders_italian_and_legacy_fallback(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="job_completed",
                title="",
                body="",
                title_key="notification.job_completed.title",
                body_key="notification.job_completed.body",
                message_params={"action": "upload", "path": "foto.txt"},
            )
            notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Historical title",
                body="Historical body",
            )
            listed = notifications.list_in_app_notifications(
                connection, user_id=self.user_id, locale="it"
            )
        keyed = next(item for item in listed if item["event"] == "job_completed")
        historical = next(
            item for item in listed if item["event"] == "upload_verified"
        )
        self.assertEqual(keyed["title"], "Job completato")
        self.assertEqual(keyed["body"], "upload: foto.txt")
        self.assertEqual(historical["title"], "Historical title")
        self.assertEqual(historical["body"], "Historical body")

    def test_mark_notification_read_persists_for_visible_member(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
            marked = notifications.mark_notification_read(
                connection,
                notification_id=item["id"],
                user_id=self.user_id,
            )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNotNone(marked)
        self.assertTrue(marked["read"])
        self.assertIsNotNone(stored["read_at"])

    def test_mark_notification_read_rejects_cross_user_without_mutation(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
            marked = notifications.mark_notification_read(
                connection,
                notification_id=item["id"],
                user_id=self.outsider_id,
            )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNone(marked)
        self.assertIsNone(stored["read_at"])

    def test_mark_notification_read_rejects_removed_member_without_mutation(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
            connection.execute(
                "DELETE FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.user_id),
            )
            marked = notifications.mark_notification_read(
                connection,
                notification_id=item["id"],
                user_id=self.user_id,
            )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNone(marked)
        self.assertIsNone(stored["read_at"])

    def test_list_filters_unread_and_read_independently(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            older_unread = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Older unread",
            )
            middle_read = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Middle read",
            )
            newer_unread = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Newer unread",
            )
            notifications.mark_notification_read(
                connection,
                notification_id=middle_read["id"],
                user_id=self.user_id,
            )

            unread = notifications.list_in_app_notifications(
                connection, user_id=self.user_id, status="unread"
            )
            read = notifications.list_in_app_notifications(
                connection, user_id=self.user_id, status="read"
            )

        self.assertEqual([item["title"] for item in unread], ["Newer unread", "Older unread"])
        self.assertEqual([item["title"] for item in read], ["Middle read"])
        self.assertTrue(all(not item["read"] for item in unread))
        self.assertTrue(all(item["read"] for item in read))
        self.assertEqual({item["id"] for item in unread}, {older_unread["id"], newer_unread["id"]})

    def test_list_before_id_bounds_incremental_history(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            ids = []
            for index in range(5):
                item = notifications.enqueue_notification(
                    connection,
                    user_id=self.user_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Item {index}",
                )
                ids.append(item["id"])
            page = notifications.list_in_app_notifications(
                connection,
                user_id=self.user_id,
                status="all",
                limit=2,
                before_id=ids[-1],
            )

        self.assertEqual([item["id"] for item in page], [ids[-2], ids[-3]])

    def test_mark_all_notifications_read_is_idempotent_and_membership_scoped(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            first = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner one",
            )
            second = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner two",
            )
            outsider = notifications.enqueue_notification(
                connection,
                user_id=self.outsider_id,
                vault_id=None,
                event="upload_verified",
                title="Outsider personal",
            )
            other_vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('hidden', 'Hidden', '/h', 'bucket', 'h', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (other_vault_id, self.user_id),
            )
            hidden = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=other_vault_id,
                event="upload_verified",
                title="Hidden by membership",
            )
            connection.execute(
                "DELETE FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (other_vault_id, self.user_id),
            )

            first_pass = notifications.mark_all_notifications_read(
                connection, user_id=self.user_id
            )
            second_pass = notifications.mark_all_notifications_read(
                connection, user_id=self.user_id
            )

            owner_rows = connection.execute(
                "SELECT id, read_at FROM notifications WHERE user_id=%s ORDER BY id",
                (self.user_id,),
            ).fetchall()
            outsider_row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (outsider["id"],),
            ).fetchone()

        self.assertEqual(first_pass["marked_count"], 2)
        self.assertEqual(first_pass["unread_count"], 0)
        self.assertEqual(second_pass["marked_count"], 0)
        self.assertEqual(second_pass["unread_count"], 0)
        by_id = {row["id"]: row["read_at"] for row in owner_rows}
        self.assertIsNotNone(by_id[first["id"]])
        self.assertIsNotNone(by_id[second["id"]])
        self.assertIsNone(by_id[hidden["id"]])
        self.assertIsNone(outsider_row["read_at"])

    def test_terminal_notification_failure_does_not_rollback_job_transition(self) -> None:
        def fail_after_sql_error(connection, *, job_id):
            del job_id
            connection.execute(
                "INSERT INTO notifications(user_id, event, title, created_at) "
                "VALUES (%s, 'job_completed', 'Job completed', 'now')",
                (self.user_id,),
            )
            connection.execute("INSERT INTO missing_notification_table(id) VALUES (1)")
            return 0

        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (self.job_id,)
            )
            with patch.object(
                notifications,
                "enqueue_job_terminal_push",
                side_effect=fail_after_sql_error,
            ):
                enqueued = notifications.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=self.job_id
                )
            self.assertEqual(enqueued, 0)

        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            notification_count = connection.execute(
                "SELECT COUNT(*) AS total FROM notifications"
            ).fetchone()["total"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(notification_count, 0)

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


class NotificationReadHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "notifications-http.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE)
                RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.outsider_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('outsider', 'Outsider', %s, FALSE)
                RETURNING id
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

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.path),
            cookie_secure=False,
            allowed_hosts="",
            trusted_proxies="",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        with SQLiteConnection(str(self.path)) as connection:
            self.owner_token = create_session(
                connection, user_id=self.owner_id, auth_method="local"
            )
            self.owner_csrf = csrf_token_for(connection, self.owner_token) or ""
            self.outsider_token = create_session(
                connection, user_id=self.outsider_id, auth_method="local"
            )
            self.outsider_csrf = csrf_token_for(connection, self.outsider_token) or ""

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self, token: str, csrf: str) -> None:
        self.client.cookies.set(self.test_settings.session_cookie_name, token)
        self.client.cookies.set(self.test_settings.csrf_cookie_name, csrf)

    def _create_notification(self) -> int:
        with SQLiteConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
        return int(item["id"])

    def test_personal_preferences_get_and_post_are_user_scoped(self) -> None:
        self._authenticate(self.owner_token, self.owner_csrf)

        initial = self.client.get("/api/vault/notification-preferences")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json(), {"items": []})

        saved = self.client.post(
            "/api/vault/notification-preferences",
            headers={"X-CSRF-Token": self.owner_csrf},
            json={
                "event": "job_completed",
                "channel": "in_app",
                "enabled": False,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["user_id"], self.owner_id)
        self.assertEqual(saved.json()["vault_id"], self.vault_id)
        self.assertFalse(saved.json()["enabled"])

        listed = self.client.get("/api/vault/notification-preferences")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"], [saved.json()])

        self._authenticate(self.outsider_token, self.outsider_csrf)
        outsider_post = self.client.post(
            "/api/vault/notification-preferences",
            headers={"X-CSRF-Token": self.outsider_csrf},
            json={
                "event": "job_completed",
                "channel": "in_app",
                "enabled": True,
            },
        )
        self.assertEqual(outsider_post.status_code, 403, outsider_post.text)
        with SQLiteConnection(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT user_id, vault_id, event, channel, enabled "
                "FROM user_vault_notification_preferences"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], self.owner_id)

    def test_personal_preferences_reject_unsupported_event_and_channel(self) -> None:
        self._authenticate(self.owner_token, self.owner_csrf)
        for payload in (
            {"event": "upload_verified", "channel": "in_app", "enabled": True},
            {"event": "job_completed", "channel": "email", "enabled": True},
        ):
            response = self.client.post(
                "/api/vault/notification-preferences",
                headers={"X-CSRF-Token": self.owner_csrf},
                json=payload,
            )
            self.assertEqual(response.status_code, 422, response.text)
        with SQLiteConnection(str(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS total "
                "FROM user_vault_notification_preferences"
            ).fetchone()["total"]
        self.assertEqual(count, 0)

    def test_notifications_http_count_is_independent_of_limit_and_push_only(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            for index in range(3):
                notifications.enqueue_notification(
                    connection,
                    user_id=self.owner_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Upload {index}",
                )
            notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="job_completed",
                title="Push only",
                channels=("push",),
            )
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.get("/api/notifications?limit=1")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertEqual(response.json()["unread_count"], 3)
        self.assertNotEqual(response.json()["items"][0]["title"], "Push only")

    def test_mark_read_persists_through_http(self) -> None:
        notification_id = self._create_notification()
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.post(
            "/api/notifications/read",
            headers={"X-CSRF-Token": self.owner_csrf},
            json={"notification_id": notification_id},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["read"])
        with SQLiteConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNotNone(row["read_at"])

    def test_cross_user_mark_read_returns_404_without_mutation(self) -> None:
        notification_id = self._create_notification()
        self._authenticate(self.outsider_token, self.outsider_csrf)

        response = self.client.post(
            "/api/notifications/read",
            headers={"X-CSRF-Token": self.outsider_csrf},
            json={"notification_id": notification_id},
        )

        self.assertEqual(response.status_code, 404, response.text)
        with SQLiteConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNone(row["read_at"])

    def test_removed_member_mark_read_returns_404_without_mutation(self) -> None:
        notification_id = self._create_notification()
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "DELETE FROM vault_members WHERE vault_id=%s AND user_id=%s",
                (self.vault_id, self.owner_id),
            )
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.post(
            "/api/notifications/read",
            headers={"X-CSRF-Token": self.owner_csrf},
            json={"notification_id": notification_id},
        )

        self.assertEqual(response.status_code, 404, response.text)
        with SQLiteConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNone(row["read_at"])

    def test_notifications_http_filters_status_and_reports_has_more(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            older = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Older unread",
            )
            middle = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Middle read",
            )
            newer = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Newer unread",
            )
            notifications.mark_notification_read(
                connection,
                notification_id=middle["id"],
                user_id=self.owner_id,
            )
        self._authenticate(self.owner_token, self.owner_csrf)

        unread = self.client.get("/api/notifications?status=unread&limit=1")
        self.assertEqual(unread.status_code, 200, unread.text)
        body = unread.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["title"], "Newer unread")
        self.assertTrue(body["has_more"])
        self.assertEqual(body["unread_count"], 2)

        older_page = self.client.get(
            f"/api/notifications?status=unread&limit=10&before_id={newer['id']}"
        )
        self.assertEqual(older_page.status_code, 200, older_page.text)
        older_body = older_page.json()
        self.assertEqual([item["id"] for item in older_body["items"]], [older["id"]])
        self.assertFalse(older_body["has_more"])

        read = self.client.get("/api/notifications?status=read")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual([item["title"] for item in read.json()["items"]], ["Middle read"])

        invalid = self.client.get("/api/notifications?status=snoozed")
        self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_mark_all_read_http_is_idempotent_and_authz_scoped(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            first = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner one",
            )
            second = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner two",
            )
            outsider = notifications.enqueue_notification(
                connection,
                user_id=self.outsider_id,
                vault_id=None,
                event="upload_verified",
                title="Outsider",
            )

        self._authenticate(self.owner_token, self.owner_csrf)
        first_response = self.client.post(
            "/api/notifications/read-all",
            headers={"X-CSRF-Token": self.owner_csrf},
        )
        self.assertEqual(first_response.status_code, 200, first_response.text)
        self.assertEqual(first_response.json()["marked_count"], 2)
        self.assertEqual(first_response.json()["unread_count"], 0)

        second_response = self.client.post(
            "/api/notifications/read-all",
            headers={"X-CSRF-Token": self.owner_csrf},
        )
        self.assertEqual(second_response.status_code, 200, second_response.text)
        self.assertEqual(second_response.json()["marked_count"], 0)
        self.assertEqual(second_response.json()["unread_count"], 0)

        with SQLiteConnection(str(self.path)) as connection:
            owner_unread = connection.execute(
                "SELECT COUNT(*) AS total FROM notifications "
                "WHERE user_id=%s AND read_at IS NULL",
                (self.owner_id,),
            ).fetchone()["total"]
            outsider_row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (outsider["id"],),
            ).fetchone()
            owner_ids = {
                row["id"]: row["read_at"]
                for row in connection.execute(
                    "SELECT id, read_at FROM notifications WHERE user_id=%s",
                    (self.owner_id,),
                ).fetchall()
            }
        self.assertEqual(owner_unread, 0)
        self.assertIsNotNone(owner_ids[first["id"]])
        self.assertIsNotNone(owner_ids[second["id"]])
        self.assertIsNone(outsider_row["read_at"])

        self._authenticate(self.outsider_token, self.outsider_csrf)
        outsider_response = self.client.post(
            "/api/notifications/read-all",
            headers={"X-CSRF-Token": self.outsider_csrf},
        )
        self.assertEqual(outsider_response.status_code, 200, outsider_response.text)
        self.assertEqual(outsider_response.json()["marked_count"], 1)


if __name__ == "__main__":
    unittest.main()
