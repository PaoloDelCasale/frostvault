"""Deployment-owned SMTP adapters and unchanged event routing (#414)."""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.catalog import ArchiveCatalog
from app.config import settings

from app.services import notifications
from app.storage import _deliver_notifications_once, set_job
from tests.test_database import run_alembic


class _SmtpRecorder(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages: list[str] = []
        self._halt = threading.Event()

    def run(self) -> None:
        self.sock.settimeout(0.3)
        while not self._halt.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (TimeoutError, socket.timeout, OSError):
                continue
            try:
                self._handle(conn)
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.sendall(b"220 frostvault-test\r\n")
        collecting = False
        buf: list[str] = []
        while True:
            line = b""
            while not line.endswith(b"\n"):
                chunk = conn.recv(1)
                if not chunk:
                    return
                line += chunk
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            upper = text.upper()
            if collecting:
                if text == ".":
                    collecting = False
                    self.messages.append("\n".join(buf))
                    conn.sendall(b"250 ok\r\n")
                else:
                    buf.append(text)
                continue
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                conn.sendall(b"250-hello\r\n250 AUTH PLAIN LOGIN\r\n")
            elif upper.startswith("MAIL") or upper.startswith("RCPT"):
                conn.sendall(b"250 ok\r\n")
            elif upper == "DATA":
                conn.sendall(b"354 go\r\n")
                collecting = True
                buf = []
            elif upper == "QUIT":
                conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def stop(self) -> None:
        self._halt.set()
        self.sock.close()
        self.join(timeout=2)


class OutboundDeliveryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "notify.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.email_settings = replace(
            settings,
            smtp_host="127.0.0.1",
            smtp_port=9,
            smtp_from_address="frostvault@example.com",
            smtp_to_address="ops@example.com",
            smtp_use_tls=False,
            smtp_username="",
            smtp_password="",
        )
        patcher = patch.object(notifications, "settings", self.email_settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')"
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                """
            )
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="a.txt", file_type="regular", size=1,
                mtime_ns=1, observed_at="2026-07-21T10:00:00+00:00",
            )
            self.job_id = int(connection.execute(
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
            ).fetchone()["id"])

    def test_smtp_adapter_delivers_to_env_recipient_not_login_address(self) -> None:
        smtp = _SmtpRecorder()
        smtp.start()
        self.addCleanup(smtp.stop)
        with patch.object(
            notifications, "settings", replace(self.email_settings, smtp_port=smtp.port)
        ), PostgresConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection, user_id=1, vault_id=2, event="job_failed",
                title="Upload failed", body="AccessDenied", job_id=self.job_id,
                channels=("in_app", "email"),
            )
            stats = notifications.deliver_pending_notifications(
                connection, smtp_client=notifications.production_smtp_client()
            )
            self.assertEqual(stats["delivered"], 1)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()["status"], "uploading")
            self.assertEqual(len(notifications.list_in_app_notifications(connection, user_id=1)), 1)
        self.assertEqual(len(smtp.messages), 1)
        self.assertIn("Job failed", smtp.messages[0])
        self.assertIn("To: ops@example.com", smtp.messages[0])
        self.assertNotIn("To: owner@example.com", smtp.messages[0])

    def test_only_terminal_upload_failure_not_routine_success_uses_email(self) -> None:
        for status in ("completed", "failed"):
            with self.subTest(status=status), patch(
                "app.storage.db", side_effect=lambda: PostgresConnection(str(self.path))
            ):
                self.assertTrue(set_job(self.job_id, status, "result"))
        with PostgresConnection(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT n.event, d.channel FROM notification_deliveries d "
                "JOIN notifications n ON n.id=d.notification_id"
            ).fetchall()
            self.assertEqual([(row["event"], row["channel"]) for row in rows], [("job_failed", "email")])
            self.assertEqual(len(notifications.list_in_app_notifications(connection, user_id=1)), 1)

    def test_existing_incidents_reach_both_channels_once(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            for incident_id, enqueue in enumerate((
                notifications.enqueue_archive_version_missing,
                notifications.enqueue_cloud_only_conflict,
            ), start=1):
                incident = {"id": incident_id, "vault_id": 2, "path": "a.txt"}
                self.assertEqual(enqueue(connection, incident=incident), 1)
                self.assertEqual(enqueue(connection, incident=incident), 0)
            self.assertEqual(len(notifications.list_in_app_notifications(connection, user_id=1)), 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) AS total FROM notification_deliveries WHERE channel='email'"
            ).fetchone()["total"], 2)

    def test_missing_or_partial_email_configuration_leaves_inbox_working(self) -> None:
        for field in ("smtp_host", "smtp_from_address", "smtp_to_address"):
            with self.subTest(field=field), patch.object(
                notifications, "settings", replace(self.email_settings, **{field: " "})
            ), PostgresConnection(str(self.path)) as connection:
                self.assertIsNone(notifications.production_smtp_client())
                notifications.enqueue_cloud_only_conflict(
                    connection, incident={"id": len(field), "vault_id": 2, "path": "a.txt"}
                )
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) AS total FROM notification_deliveries"
                ).fetchone()["total"], 0)
                self.assertGreater(notifications.count_unread_notifications(connection, user_id=1), 0)

    def test_worker_uses_production_smtp_client(self) -> None:
        smtp = _SmtpRecorder()
        smtp.start()
        self.addCleanup(smtp.stop)
        with PostgresConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection, user_id=1, event="job_failed", title="failed",
                channels=("in_app", "email"),
            )
        with patch.object(
            notifications, "settings", replace(self.email_settings, smtp_port=smtp.port)
        ), patch("app.storage.db", side_effect=lambda: PostgresConnection(str(self.path))), patch(
            "app.services.protection_notifications.maintain_notifications"
        ):
            _deliver_notifications_once()
        with PostgresConnection(str(self.path)) as connection:
            delivery = connection.execute(
                "SELECT status, attempt_count FROM notification_deliveries"
            ).fetchone()
        self.assertEqual(delivery["status"], "delivered")
        self.assertEqual(delivery["attempt_count"], 1)
        self.assertEqual(len(smtp.messages), 1)

    def test_retry_backoff_is_in_the_future_and_errors_are_redacted(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            notifications.enqueue_notification(
                connection, user_id=1, event="job_failed", title="failed",
                channels=("in_app", "email"),
            )
            with patch("app.services.notifications.smtplib.SMTP", side_effect=RuntimeError("transport detail")):
                stats = notifications.deliver_pending_notifications(
                    connection, smtp_client=notifications.production_smtp_client()
                )
            delivery = connection.execute("SELECT * FROM notification_deliveries").fetchone()
            self.assertEqual(stats["requeued"], 1)
            self.assertEqual(delivery["status"], "pending")
            self.assertGreater(delivery["next_attempt_at"], delivery["updated_at"])
            self.assertEqual(delivery["last_error"], "smtp delivery failed")
            self.assertEqual(notifications.count_unread_notifications(connection, user_id=1), 1)

    def test_smtp_tls_modes_and_login(self) -> None:
        for port in (587, 465):
            with self.subTest(port=port), patch("app.services.notifications.smtplib.SMTP") as plain, patch("app.services.notifications.smtplib.SMTP_SSL") as secure:
                factory = secure if port == 465 else plain
                smtp = MagicMock()
                factory.return_value = smtp
                notifications.SmtpNotificationClient().send({
                    "host": "smtp.example.test", "port": port,
                    "from_address": "alerts@example.com", "to_address": "owner@example.com",
                    "username": "alerts", "password": "test-only-password", "use_tls": True,
                })
                factory.assert_called_once()
                (plain if port == 465 else secure).assert_not_called()
                smtp.login.assert_called_once_with("alerts", "test-only-password")
                smtp.send_message.assert_called_once()
                if port == 465:
                    smtp.starttls.assert_not_called()
                else:
                    smtp.starttls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
