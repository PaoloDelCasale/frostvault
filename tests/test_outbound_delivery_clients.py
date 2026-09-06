"""Production SMTP/webhook adapters and Job-failed routing (issue #301)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
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
from app.services import notifications
from app.storage import _deliver_notifications_once, set_job
from tests.test_database import run_alembic


class _WebhookRecorder(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _WebhookHandler)
        self.payloads: list[dict] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/hook"


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        server = self.server
        assert isinstance(server, _WebhookRecorder)
        server.payloads.append(json.loads(raw.decode("utf-8")))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


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
        try:
            self.sock.close()
        except OSError:
            pass
        self.join(timeout=2)


class OutboundDeliveryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "notify.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin, active)
                VALUES (1, 'owner@example.com', 'Owner', 'hash', TRUE, TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
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

    def test_http_and_smtp_adapters_deliver_from_admin_config(self) -> None:
        webhook = _WebhookRecorder()
        smtp = _SmtpRecorder()
        webhook.start()
        smtp.start()
        self.addCleanup(webhook.stop)
        self.addCleanup(smtp.stop)

        with SQLiteConnection(str(self.path)) as connection:
            notifications.set_global_webhook_endpoint(
                connection, url=webhook.url, enabled=True
            )
            notifications.set_global_smtp_endpoint(
                connection,
                host="127.0.0.1",
                port=smtp.port,
                from_address="frostvault@example.com",
                to_address="ops@example.com",
                use_tls=False,
                enabled=True,
            )
            notifications.enqueue_notification(
                connection,
                user_id=1,
                vault_id=2,
                event="job_failed",
                title="Upload failed",
                body="AccessDenied",
                job_id=self.job_id,
                channels=("in_app", "email", "webhook"),
            )
            webhook_client, smtp_client = notifications.production_delivery_clients(
                connection
            )
            stats = notifications.deliver_pending_notifications(
                connection,
                webhook_client=webhook_client,
                smtp_client=smtp_client,
            )
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            deliveries = connection.execute(
                "SELECT channel, status FROM notification_deliveries ORDER BY channel"
            ).fetchall()

        self.assertEqual(stats["delivered"], 2)
        self.assertEqual(job["status"], "uploading")
        self.assertEqual(
            [(row["channel"], row["status"]) for row in deliveries],
            [("email", "delivered"), ("webhook", "delivered")],
        )
        self.assertEqual(len(webhook.payloads), 1)
        self.assertEqual(webhook.payloads[0]["event"], "job_failed")
        self.assertEqual(len(smtp.messages), 1)
        self.assertIn("Job failed", smtp.messages[0])

    def test_job_failed_routes_to_configured_email_and_webhook(self) -> None:
        with patch(
            "app.storage.db",
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ), SQLiteConnection(str(self.path)) as connection:
            notifications.set_global_webhook_endpoint(
                connection, url="http://127.0.0.1:9/hook", enabled=True
            )
            notifications.set_global_smtp_endpoint(
                connection,
                host="127.0.0.1",
                port=9,
                from_address="frostvault@example.com",
                use_tls=False,
                enabled=True,
            )
        with patch(
            "app.storage.db",
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ):
            self.assertTrue(set_job(self.job_id, "failed", "AccessDenied"))
        with SQLiteConnection(str(self.path)) as connection:
            channels = [
                row["channel"]
                for row in connection.execute(
                    """
                    SELECT channel FROM notification_deliveries
                    ORDER BY channel
                    """
                ).fetchall()
            ]
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(channels, ["email", "webhook"])

    def test_worker_uses_production_clients_and_backoff(self) -> None:
        webhook = _WebhookRecorder()
        webhook.start()
        self.addCleanup(webhook.stop)
        with SQLiteConnection(str(self.path)) as connection:
            notifications.set_global_webhook_endpoint(
                connection, url=webhook.url, enabled=True
            )
            notifications.enqueue_notification(
                connection,
                user_id=1,
                vault_id=2,
                event="job_failed",
                title="failed",
                job_id=self.job_id,
                channels=("webhook",),
            )
        with patch(
            "app.storage.db",
            side_effect=lambda: SQLiteConnection(str(self.path)),
        ), patch(
            "app.storage.notification_service.reconcile_pending_terminal_notifications"
        ):
            _deliver_notifications_once()
        with SQLiteConnection(str(self.path)) as connection:
            delivery = connection.execute(
                """
                SELECT status, attempt_count, next_attempt_at
                FROM notification_deliveries WHERE channel='webhook'
                """
            ).fetchone()
        self.assertEqual(delivery["status"], "delivered")
        self.assertEqual(delivery["attempt_count"], 1)
        self.assertEqual(len(webhook.payloads), 1)

    def test_retry_backoff_is_in_the_future(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            notifications.set_global_webhook_endpoint(
                connection, url="http://127.0.0.1:1/missing", enabled=True
            )
            notifications.enqueue_notification(
                connection,
                user_id=1,
                vault_id=2,
                event="job_failed",
                title="failed",
                job_id=self.job_id,
                channels=("webhook",),
            )
            client = notifications.HttpWebhookClient()
            client.timeout = 0.2
            stats = notifications.deliver_pending_notifications(
                connection,
                webhook_client=client,
                max_attempts=3,
            )
            delivery = connection.execute(
                """
                SELECT status, attempt_count, next_attempt_at, last_error
                FROM notification_deliveries
                """
            ).fetchone()
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(delivery["status"], "pending")
        self.assertEqual(delivery["attempt_count"], 1)
        self.assertIsNotNone(delivery["next_attempt_at"])
        self.assertNotEqual(delivery["next_attempt_at"], "")
        self.assertEqual(delivery["last_error"], "webhook delivery failed")
        self.assertEqual(job["status"], "uploading")


if __name__ == "__main__":
    unittest.main()
