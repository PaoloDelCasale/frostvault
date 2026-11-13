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
from app.services import notifications
from app.sessions import create_session, csrf_token_for
from tests.owner_fixtures import insert_job, insert_owner, insert_vault
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
            insert_owner(connection)
            self.vault_id = insert_vault(connection)["id"]
            self.job_id = insert_job(connection, vault_id=self.vault_id)

    def test_enqueue_creates_retrievable_in_app_notification(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="vault_root_relocated",
                title="Membership changed",
                body="A viewer was added",
                job_id=self.job_id,
            )
            listed = notifications.list_in_app_notifications(
                connection
            )
        self.assertEqual(item["event"], "vault_root_relocated")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "Membership changed")
        self.assertFalse(listed[0]["read"])

    def test_terminal_events_always_reach_the_owner_inbox_without_email(self) -> None:
        with patch.object(notifications, "settings", replace(settings, smtp_host="")):
            with PostgresConnection(str(self.path)) as connection:
                for action, status, event in (
                    ("recover", "completed", "recovery_completed"), ("recover", "failed", "recovery_failed"),
                ):
                    # A Job keeps its action for life: one Job per outcome.
                    job_id = insert_job(
                        connection, vault_id=self.vault_id, action=action, status=status,
                    )
                    self.assertEqual(
                        notifications.enqueue_job_terminal_notification(
                            connection, job_id=job_id
                        ), 1,
                    )
                    row = connection.execute(
                        "SELECT in_app_enabled, dedupe_key FROM notifications "
                        "WHERE job_id=%s AND event=%s",
                        (job_id, event),
                    ).fetchone()
                    self.assertTrue(row["in_app_enabled"])
                    self.assertEqual(row["dedupe_key"], f"job:{job_id}:{event}")
                self.assertEqual(len(notifications.list_in_app_notifications(
                    connection
                )), 2)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS total FROM notification_deliveries"
                ).fetchone()["total"], 0)

    def test_failed_upload_reaches_the_owner_inbox_as_an_open_problem(self) -> None:
        with patch.object(notifications, "settings", replace(settings, smtp_host="")):
            with PostgresConnection(str(self.path)) as connection:
                # A terminal Job is final: the failure is a Job of its own.
                job_id = insert_job(connection, vault_id=self.vault_id, path="b.txt", status="failed")
                self.assertEqual(notifications.enqueue_job_terminal_notification(
                    connection, job_id=job_id
                ), 1)
                row = connection.execute(
                    "SELECT in_app_enabled, dedupe_key, problem_status FROM notifications "
                    "WHERE job_id=%s AND event='job_failed'",
                    (job_id,),
                ).fetchone()
        self.assertTrue(row["in_app_enabled"])
        self.assertEqual(row["dedupe_key"], f"job:{job_id}:job_failed")
        self.assertEqual(row["problem_status"], "open")

    def test_unread_count_ignores_limit_and_historical_email_only_rows(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for index in range(3):
                notifications.enqueue_notification(
                    connection, vault_id=self.vault_id,
                    event="upload_verified", title=f"Upload {index}",
                )
            notifications.enqueue_notification(
                connection, vault_id=self.vault_id,
                event="recovery_completed", title="Email only", channels=("email",),
            )
            listed = notifications.list_in_app_notifications(
                connection, limit=1
            )
            unread_count = notifications.count_unread_notifications(
                connection
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
                job_id = insert_job(
                    connection, vault_id=self.vault_id, action="recover", status="failed",
                )
                first = notifications.enqueue_job_terminal_notification(
                    connection, job_id=job_id
                )
                second = notifications.enqueue_job_terminal_notification(
                    connection, job_id=job_id
                )
                rows = connection.execute(
                    "SELECT id, dedupe_key FROM notifications WHERE job_id=%s",
                    (job_id,),
                ).fetchall()
                deliveries = connection.execute(
                    "SELECT channel FROM notification_deliveries"
                ).fetchall()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], f"job:{job_id}:recovery_failed")
        self.assertEqual([row["channel"] for row in deliveries], ["email"])

    def test_removed_channels_are_rejected_before_writing_any_notification(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for channel in ("webhook", "push"):
                with self.subTest(channel=channel), self.assertRaises(ValueError):
                    notifications.enqueue_notification(
                        connection, event="recovery_completed",
                        title="Unsupported", channels=("in_app", channel),
                    )
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS total FROM notifications"
            ).fetchone()["total"], 0)

    def test_keyed_terminal_notification_renders_italian_and_legacy_fallback(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection,
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
                vault_id=self.vault_id,
                event="upload_verified",
                title="Historical title",
                body="Historical body",
            )
            listed = notifications.list_in_app_notifications(
                connection, locale="it"
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
                vault_id=self.vault_id,
                event="upload_verified",
                title="Upload verified",
                body="ok",
            )
            marked = notifications.mark_notification_read(
                connection,
                notification_id=item["id"],
            )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNotNone(marked)
        self.assertTrue(marked["read"])
        self.assertIsNotNone(stored["read_at"])

    def test_mark_notification_read_rejects_hidden_and_unknown_without_mutation(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="recovery_completed",
                title="Email only",
                channels=("email",),
            )
            marked = notifications.mark_notification_read(
                connection,
                notification_id=item["id"],
            )
            unknown = notifications.mark_notification_read(
                connection,
                notification_id=item["id"] + 100,
            )
            stored = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s", (item["id"],)
            ).fetchone()

        self.assertIsNone(marked)
        self.assertIsNone(unknown)
        self.assertIsNone(stored["read_at"])

    def test_notifications_have_no_user_and_dedupe_by_key(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            first = notifications.enqueue_notification(
                connection, event="weekly_summary", title="Week",
                message_params={}, dedupe_key="weekly:2026-46",
            )
            again = notifications.enqueue_notification(
                connection, event="weekly_summary", title="Week",
                message_params={}, dedupe_key="weekly:2026-46",
                channels=("in_app", "email"),
            )
            total = connection.execute(
                "SELECT count(*) AS total FROM notifications"
            ).fetchone()["total"]
            deliveries = connection.execute(
                "SELECT notification_id, channel FROM notification_deliveries"
            ).fetchall()
            columns = {row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('notifications', 'notification_deliveries')"
            ).fetchall()}
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(total, 1)
        self.assertEqual(deliveries, [{"notification_id": first["id"], "channel": "email"}])
        self.assertNotIn("user_id", columns)

    def test_list_filters_unread_and_read_independently(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            older_unread = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Older unread",
            )
            middle_read = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Middle read",
            )
            newer_unread = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Newer unread",
            )
            notifications.mark_notification_read(
                connection,
                notification_id=middle_read["id"],
            )

            unread = notifications.list_in_app_notifications(
                connection, status="unread"
            )
            read = notifications.list_in_app_notifications(
                connection, status="read"
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
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Item {index}",
                )
                ids.append(item["id"])
            page = notifications.list_in_app_notifications(
                connection,
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
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Bulk {index}",
                )
            # Endpoint fetches limit+1; the service must not clamp the sentinel
            # fetch back down to 200 or has_more stays false forever.
            fetched = notifications.list_in_app_notifications(
                connection,
                status="unread",
                limit=201,
            )

        self.assertEqual(len(fetched), 201)

    def test_mark_all_notifications_read_is_idempotent_and_skips_email_only(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            first = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner one",
            )
            second = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner two",
            )
            email_only = notifications.enqueue_notification(
                connection,
                vault_id=None,
                event="recovery_completed",
                title="Email only",
                channels=("email",),
            )

            first_pass = notifications.mark_all_notifications_read(
                connection
            )
            second_pass = notifications.mark_all_notifications_read(
                connection
            )

            owner_rows = connection.execute(
                "SELECT id, read_at FROM notifications WHERE in_app_enabled ORDER BY id",
            ).fetchall()
            email_only_row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (email_only["id"],),
            ).fetchone()

        self.assertEqual(first_pass["marked_count"], 2)
        self.assertEqual(first_pass["unread_count"], 0)
        self.assertEqual(second_pass["marked_count"], 0)
        self.assertEqual(second_pass["unread_count"], 0)
        by_id = {row["id"]: row["read_at"] for row in owner_rows}
        self.assertIsNotNone(by_id[first["id"]])
        self.assertIsNotNone(by_id[second["id"]])
        self.assertIsNone(email_only_row["read_at"])

    def test_terminal_notification_failure_does_not_rollback_job_transition(self) -> None:
        def fail_after_sql_error(connection, *, job_id):
            del job_id
            connection.execute(
                "INSERT INTO notifications(event, title) "
                "VALUES ('recovery_completed', 'Recovery completed')"
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
                    connection, vault_id=self.vault_id,
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
                    connection
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
            insert_owner(connection)
            self.vault_id = insert_vault(connection)["id"]

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
            self.owner_token = create_session(connection, auth_method="local")
            self.owner_csrf = csrf_token_for(connection, self.owner_token) or ""

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def _authenticate(self, token: str, csrf: str) -> None:
        self.client.cookies.set(self.test_settings.session_cookie_name, token)
        self.client.cookies.set(self.test_settings.csrf_cookie_name, csrf)

    def _create_notification(self) -> int:
        with PostgresConnection(str(self.path)) as connection:
            item = notifications.enqueue_notification(
                connection,
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
                    vault_id=self.vault_id,
                    event="upload_verified",
                    title=f"Upload {index}",
                )
            notifications.enqueue_notification(
                connection,
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
            notifications.mark_notification_read(connection, notification_id=item['id'])
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

    def test_unknown_notification_mark_read_returns_404_without_mutation(self) -> None:
        notification_id = self._create_notification()
        self._authenticate(self.owner_token, self.owner_csrf)

        response = self.client.post(
            "/api/notifications/read",
            headers={"X-CSRF-Token": self.owner_csrf},
            json={"notification_id": notification_id + 100},
        )

        self.assertEqual(response.status_code, 404, response.text)
        with PostgresConnection(str(self.path)) as connection:
            row = connection.execute(
                "SELECT read_at FROM notifications WHERE id=%s",
                (notification_id,),
            ).fetchone()
        self.assertIsNone(row["read_at"])

    def test_notifications_require_a_session(self) -> None:
        for method, path in (
            ("GET", "/api/notifications"),
            ("POST", "/api/notifications/read"),
            ("POST", "/api/notifications/read-all"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path, json={"notification_id": 1})
                self.assertEqual(response.status_code, 401, response.text)

    def test_notifications_http_has_more_at_max_limit(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for index in range(201):
                notifications.enqueue_notification(
                    connection,
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
                vault_id=self.vault_id,
                event="upload_verified",
                title="Older unread",
            )
            middle = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Middle read",
            )
            newer = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Newer unread",
            )
            notifications.mark_notification_read(
                connection,
                notification_id=middle["id"],
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

    def test_mark_all_read_http_is_idempotent_and_skips_email_only(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            first = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner one",
            )
            second = notifications.enqueue_notification(
                connection,
                vault_id=self.vault_id,
                event="upload_verified",
                title="Owner two",
            )
            email_only = notifications.enqueue_notification(
                connection,
                vault_id=None,
                event="recovery_completed",
                title="Email only",
                channels=("email",),
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
            rows = {
                row["id"]: row["read_at"]
                for row in connection.execute("SELECT id, read_at FROM notifications").fetchall()
            }
        self.assertIsNotNone(rows[first["id"]])
        self.assertIsNotNone(rows[second["id"]])
        self.assertIsNone(rows[email_only["id"]])

if __name__ == "__main__":
    unittest.main()
