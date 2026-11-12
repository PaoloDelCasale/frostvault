"""In-app notifications and bounded delivery retries (issue #16).

Seams under test:
- ``enqueue_notification`` / ``list_in_app_notifications``
- ``deliver_pending_notifications`` with an injectable SMTP adapter
- Delivery retries are bounded and never mutate the underlying job status
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings

from app.main import app
from app.security import hash_password
from app.services import notifications
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class _FailingSmtp:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: dict) -> None:
        self.calls += 1
        raise RuntimeError("smtp down")


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "notify.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', %s)
                """,
                (hash_password("owner-password-1"),),
            )
            self.user_id = connection.execute(
                "SELECT id FROM owner WHERE id=1"
            ).fetchone()["id"]
            self.outsider_id = 2  # forbidden principal; never inserted
            connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs')
                """
            )
            self.vault_id = connection.execute(
                "SELECT id FROM vaults WHERE slug='docs'"
            ).fetchone()["id"]
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
                    vault_id, vault_file_id, path, action, status,
                    requested_at, updated_at
                ) VALUES (
                    %s, %s, 'a.txt', 'upload', 'completed',
                    '2026-07-22T00:00:00+00:00', '2026-07-22T00:00:00+00:00'
                )
                """,
                (self.vault_id, vault_file["id"],),
            )
            self.job_id = connection.execute(
                "SELECT id FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    def test_enqueue_creates_retrievable_in_app_notification(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="vault_root_relocated",
                title="Membership changed",
                body="A viewer was added",
                job_id=self.job_id,
            )
            listed = notifications.list_in_app_notifications(
                connection, user_id=self.user_id
            )
        self.assertEqual(item["event"], "vault_root_relocated")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "Membership changed")
        self.assertFalse(listed[0]["read"])

    def test_terminal_events_always_reach_the_owner_inbox_without_email(self) -> None:
        with patch.object(notifications, "settings", replace(settings, smtp_host="")):
            with PostgresConnection(str(self.path)) as connection:
                for action, status, event in (
                    ("recover", "completed", "recovery_completed"), ("upload", "failed", "job_failed"),
                ):
                    connection.execute(
                        "UPDATE jobs SET action=%s, status=%s WHERE id=%s", (action, status, self.job_id)
                    )
                    self.assertEqual(
                        notifications.enqueue_job_terminal_notification(
                            connection, job_id=self.job_id
                        ), 1,
                    )
                    row = connection.execute(
                        'SELECT in_app_enabled, dedupe_key FROM notifications WHERE job_id=%s AND event=%s',
                        (self.job_id, event,),
                    ).fetchone()
                    self.assertTrue(row["in_app_enabled"])
                    self.assertEqual(row["dedupe_key"], f"job:{self.job_id}:{event}")
                self.assertEqual(len(notifications.list_in_app_notifications(
                    connection, user_id=self.user_id
                )), 2)
                with self.assertRaises(ValueError):
                    notifications.list_in_app_notifications(connection, user_id=self.outsider_id)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS total FROM notification_deliveries"
                ).fetchone()["total"], 0)

    def test_unread_count_ignores_limit_and_historical_email_only_rows(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for index in range(3):
                notifications.enqueue_notification(
                    connection, user_id=self.user_id, vault_id=self.vault_id,
                    event="upload_verified", title=f"Upload {index}",
                )
            notifications.enqueue_notification(
                connection, user_id=self.user_id, vault_id=self.vault_id,
                event="recovery_completed", title="Email only", channels=("email",),
            )
            listed = notifications.list_in_app_notifications(
                connection, user_id=self.user_id, limit=1
            )
            unread_count = notifications.count_unread_notifications(
                connection, user_id=self.user_id
            )
        self.assertEqual(len(listed), 1)
        self.assertEqual(unread_count, 3)
        self.assertNotIn("Email only", {item["title"] for item in listed})

    def test_terminal_notification_deduplicates_canonical_row_and_delivery(self) -> None:
        email_settings = replace(
            settings, smtp_host="smtp.example.test",
            smtp_from_address="alerts@example.com", smtp_to_address="owner@example.com",
        )
        with patch.object(notifications, "settings", email_settings):
            with PostgresConnection(str(self.path)) as connection:
                connection.execute(
                    "UPDATE jobs SET status='failed' WHERE id=%s", (self.job_id,)
                )
                first = notifications.enqueue_job_terminal_notification(
                    connection, job_id=self.job_id
                )
                second = notifications.enqueue_job_terminal_notification(
                    connection, job_id=self.job_id
                )
                rows = connection.execute(
                    "SELECT id, dedupe_key FROM notifications WHERE job_id=%s",
                    (self.job_id,),
                ).fetchall()
                deliveries = connection.execute(
                    "SELECT channel FROM notification_deliveries"
                ).fetchall()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], f"job:{self.job_id}:job_failed")
        self.assertEqual([row["channel"] for row in deliveries], ["email"])

    def test_removed_channels_are_rejected_before_writing_any_notification(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for channel in ("webhook", "push"):
                with self.subTest(channel=channel), self.assertRaises(ValueError):
                    notifications.enqueue_notification(
                        connection, user_id=self.user_id, event="recovery_completed",
                        title="Unsupported", channels=("in_app", channel),
                    )
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS total FROM notifications"
            ).fetchone()["total"], 0)

    def test_keyed_terminal_notification_renders_italian_and_legacy_fallback(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="recovery_completed",
                title="",
                body="",
                title_key="notification.recovery_completed.title",
                body_key="notification.recovery_completed.body",
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
        keyed = next(item for item in listed if item["event"] == "recovery_completed")
        historical = next(
            item for item in listed if item["event"] == "upload_verified"
        )
        self.assertEqual(keyed["title"], "Recupero completato")
        self.assertEqual(keyed["body"], "foto.txt è di nuovo sul server.")
        self.assertEqual(historical["title"], "Historical title")
        self.assertEqual(historical["body"], "Historical body")

    def test_mark_notification_read_persists_for_visible_member(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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

    def test_mark_notification_read_rejects_non_owner_without_mutation(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.user_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
            with self.assertRaises(ValueError):
                notifications.mark_notification_read(
                    connection, notification_id=item["id"], user_id=self.outsider_id,
                )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNone(stored["read_at"])

    def test_list_filters_unread_and_read_independently(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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
        with PostgresConnection(str(self.path)) as connection:
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

    def test_list_has_more_sentinel_works_at_max_public_page_size(self) -> None:
        """Public limit=200 must still detect a 201st row via internal sentinel."""
        with PostgresConnection(str(self.path)) as connection:
            for index in range(201):
                notifications.enqueue_notification(
                    connection,
                    user_id=self.user_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Bulk {index}",
                )
            # Endpoint fetches limit+1; the service must not clamp the sentinel
            # fetch back down to 200 or has_more stays false forever.
            fetched = notifications.list_in_app_notifications(
                connection,
                user_id=self.user_id,
                status="unread",
                limit=201,
            )

        self.assertEqual(len(fetched), 201)

    def test_mark_all_notifications_read_is_idempotent_and_preserves_email_only_history(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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
                user_id=1,
                channels=("email",),
                vault_id=None,
                event="upload_verified",
                title="Outsider personal",
            )

            first_pass = notifications.mark_all_notifications_read(
                connection, user_id=self.user_id
            )
            second_pass = notifications.mark_all_notifications_read(
                connection, user_id=self.user_id
            )

            owner_rows = connection.execute(
                "SELECT id, read_at FROM notifications ORDER BY id",
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
        self.assertIsNone(outsider_row["read_at"])

    def test_terminal_notification_failure_does_not_rollback_job_transition(self) -> None:
        def fail_after_sql_error(connection, *, job_id):
            del job_id
            connection.execute(
                "INSERT INTO notifications( event, title, created_at) VALUES ( 'recovery_completed', 'Recovery completed', 'now')",
                (),
            )
            connection.execute("INSERT INTO missing_notification_table(id) VALUES (1)")
            return 0

        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE jobs SET status='completed' WHERE id=%s", (self.job_id,)
            )
            with patch.object(
                notifications,
                "enqueue_job_terminal_notification",
                side_effect=fail_after_sql_error,
            ):
                enqueued = notifications.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=self.job_id
                )
            self.assertIsNone(enqueued)

        with PostgresConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            notification_count = connection.execute(
                "SELECT COUNT(*) AS total FROM notifications"
            ).fetchone()["total"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(notification_count, 0)

    def test_email_retries_are_bounded_and_do_not_change_job_result_or_inbox(self) -> None:
        smtp = _FailingSmtp()
        email_settings = replace(
            settings, smtp_host="smtp.example.test",
            smtp_from_address="alerts@example.com", smtp_to_address="owner@example.com",
        )
        with patch.object(notifications, "settings", email_settings):
            with PostgresConnection(str(self.path)) as connection:
                notifications.enqueue_notification(
                    connection, user_id=self.user_id, vault_id=self.vault_id,
                    event="upload_verified", title="Upload verified",
                    job_id=self.job_id, channels=("in_app", "email"),
                )
                # Four delivery passes with max_attempts=3 must stop retrying.
                for _ in range(4):
                    connection.execute(
                        "UPDATE notification_deliveries "
                        "SET next_attempt_at='2000-01-01T00:00:00+00:00' "
                        "WHERE status='pending'"
                    )
                    notifications.deliver_pending_notifications(
                        connection, smtp_client=smtp, max_attempts=3
                    )
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
                ).fetchone()
                delivery = connection.execute(
                    "SELECT status, attempt_count FROM notification_deliveries"
                ).fetchone()
                self.assertEqual(len(notifications.list_in_app_notifications(
                    connection, user_id=self.user_id
                )), 1)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(delivery["status"], "failed")
        self.assertEqual(delivery["attempt_count"], 3)
        self.assertEqual(smtp.calls, 3)


class NotificationReadHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "notifications-http.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with PostgresConnection(str(self.path)) as connection:
            self.owner_id = connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', %s)
                RETURNING id
                """,
                (hash_password(self.PASSWORD),),
            ).fetchone()["id"]
            self.outsider_id = 2  # forbidden principal; never inserted
            self.vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES ('docs', 'Docs', '/src', 'bucket', 'docs')
                RETURNING id
                """
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            database_dsn=database_dsn(str(self.path)),
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

        with PostgresConnection(str(self.path)) as connection:
            self.owner_token = create_session(
                connection, user_id=self.owner_id, auth_method="local"
            )
            self.owner_csrf = csrf_token_for(connection, self.owner_token) or ""
            with self.assertRaises(ValueError):
                create_session(connection, user_id=2, auth_method="local")
            self.outsider_token = "invalid-session"
            self.outsider_csrf = "invalid-csrf"

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self, token: str, csrf: str) -> None:
        self.client.cookies.set(self.test_settings.session_cookie_name, token)
        self.client.cookies.set(self.test_settings.csrf_cookie_name, csrf)

    def _create_notification(self) -> int:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                user_id=self.owner_id,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
        return int(item["id"])

    def test_notifications_http_count_is_independent_of_limit_and_email_only(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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
                event="recovery_completed",
                title="Email only",
                channels=("email",),
            )
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.get("/api/notifications?limit=1")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertEqual(response.json()["unread_count"], 3)
        self.assertNotEqual(response.json()["items"][0]["title"], "Email only")

    def test_problem_and_unread_counters_are_independent_and_email_status_is_safe(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.set_problem(connection, key='test-blocked', event='protection_blocked',
                                             vault_id=self.vault_id, active=True, params={'component':'cloud'})
            notifications.mark_notification_read(connection, notification_id=item['id'], user_id=self.owner_id)
        self._authenticate(self.owner_token, self.owner_csrf)
        with patch.object(notifications, 'settings', replace(settings, smtp_host='')):
            response = self.client.get('/api/notifications?limit=1')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['unread_count'], 0)
        self.assertEqual(data['open_problem_count'], 1)
        self.assertFalse(data['email_configured'])
        self.assertEqual(data['items'][0]['problem_status'], 'open')
        self.assertNotIn('smtp_password', data)

    def test_removed_notification_routes_are_not_exposed(self) -> None:
        self._authenticate(self.owner_token, self.owner_csrf)
        for path, methods in (
            ("/api/push/config", ("GET",)),
            ("/api/push/subscriptions", ("POST",)),
            ("/api/admin/notification-endpoints/webhook", ("POST",)),
            ("/api/admin/notification-endpoints/smtp", ("POST",)),
            ("/api/vault/notification-preferences", ("GET", "POST")),
        ):
            self.assertNotIn(path, app.openapi()["paths"])
            for method in methods:
                with self.subTest(path=path, method=method):
                    response = self.client.request(
                        method, path, headers={"X-CSRF-Token": self.owner_csrf}
                    )
                    self.assertEqual(response.status_code, 404, response.text)

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
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNotNone(row["read_at"])

    def test_invalid_session_cannot_mark_read_without_mutation(self) -> None:
        notification_id = self._create_notification()
        self._authenticate(self.outsider_token, self.outsider_csrf)

        response = self.client.post(
            "/api/notifications/read",
            headers={"X-CSRF-Token": self.outsider_csrf},
            json={"notification_id": notification_id},
        )

        self.assertEqual(response.status_code, 403, response.text)
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNone(row["read_at"])

    def test_notifications_http_has_more_at_max_limit(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for index in range(201):
                notifications.enqueue_notification(
                    connection,
                    user_id=self.owner_id,
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Max page {index}",
                )
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.get("/api/notifications?status=unread&limit=200")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["items"]), 200)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["unread_count"], 201)

    def test_notifications_http_filters_status_and_reports_has_more(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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

    def test_mark_all_read_http_is_idempotent_and_requires_live_session(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
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
                user_id=1,
                channels=("email",),
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

        with PostgresConnection(str(self.path)) as connection:
            owner_unread = connection.execute(
                'SELECT COUNT(*) AS total FROM notifications WHERE in_app_enabled=TRUE AND read_at IS NULL',
                (),
            ).fetchone()["total"]
            outsider_row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (outsider["id"],),
            ).fetchone()
            owner_ids = {
                row["id"]: row["read_at"]
                for row in connection.execute(
                    "SELECT id, read_at FROM notifications",
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
        self.assertEqual(outsider_response.status_code, 403, outsider_response.text)


if __name__ == "__main__":
    unittest.main()
