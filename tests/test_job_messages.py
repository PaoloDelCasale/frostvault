"""Job messages persist as stable keys and localize at the jobs API seam.

Seams under test:
- ``storage.set_job`` / ``schedule_upload_retry`` persist ``message_key`` + params
- ``app.i18n.present_job_message`` / ``/api/jobs`` localize for the request locale
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.i18n import LOCALE_COOKIE_NAME, present_job_message
from app.main import app
from app.security import hash_password
from app.sessions import create_session
from app.storage import schedule_upload_retry, set_job
from tests.test_database import run_alembic


class PresentJobMessageTests(unittest.TestCase):
    def test_keyed_message_localizes_with_params(self) -> None:
        row = {
            "message_key": "job.recovered_to",
            "message_params": '{"target": "/data/a.txt"}',
            "message": "Recovered to /data/a.txt",
        }
        self.assertEqual(
            present_job_message(row, "it"),
            "Recuperato in /data/a.txt",
        )
        self.assertEqual(
            present_job_message(row, "en"),
            "Recovered to /data/a.txt",
        )

    def test_legacy_prose_message_is_returned_unchanged(self) -> None:
        row = {
            "message_key": None,
            "message_params": None,
            "message": "Cloud copy digest does not match local file",
        }
        self.assertEqual(
            present_job_message(row, "it"),
            "Cloud copy digest does not match local file",
        )


class JobMessagePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'hash', TRUE)
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
                """
                INSERT INTO vault_members(vault_id, user_id, role)
                VALUES (2, 1, 'owner')
                """
            )
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path="a.txt",
                file_type="regular",
                size=3,
                mtime_ns=1,
                observed_at="2026-07-22T10:00:00+00:00",
            )
            self.job_id = connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status, requested_by,
                    requested_at, updated_at
                ) VALUES (
                    2,
                    (SELECT id FROM vault_files WHERE vault_id=2 LIMIT 1),
                    'a.txt', 'upload', 'queued', 1,
                    '2026-07-22T10:00:00+00:00', '2026-07-22T10:00:00+00:00'
                )
                RETURNING id
                """
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
        )
        for target in ("app.database.settings", "app.storage.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_set_job_stores_message_key_and_english_fallback(self) -> None:
        set_job(
            self.job_id,
            "completed",
            message_key="job.upload_verified",
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT status, message, message_key, message_params FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            notification = connection.execute(
                "SELECT event, in_app_enabled, dedupe_key FROM notifications "
                "WHERE job_id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["message_key"], "job.upload_verified")
        self.assertEqual(row["message"], "Upload verified")
        self.assertEqual(row["message_params"], "{}")
        self.assertEqual(notification["event"], "job_completed")
        self.assertTrue(notification["in_app_enabled"])
        self.assertEqual(
            notification["dedupe_key"], f"job:{self.job_id}:job_completed"
        )

    def test_set_job_keeps_terminal_state_when_notification_sql_fails(self) -> None:
        def fail_notification(connection, *, job_id):
            del job_id
            connection.execute("INSERT INTO missing_notification_table(id) VALUES (1)")

        with patch(
            "app.storage.notification_service.enqueue_job_terminal_push",
            side_effect=fail_notification,
        ):
            set_job(self.job_id, "completed", message_key="job.upload_verified")

        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_schedule_upload_retry_stores_transient_key(self) -> None:
        schedule_upload_retry(
            self.job_id,
            message_key="job.retrying_transient",
            message_params={"error": "SlowDown"},
            retry_count=1,
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT status, message, message_key, message_params FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["message_key"], "job.retrying_transient")
        self.assertEqual(row["message"], "Retrying after transient error: SlowDown")
        self.assertIn("SlowDown", row["message_params"])


class JobMessageHttpTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            password_hash = hash_password(self.PASSWORD)
            user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, TRUE)
                RETURNING id
                """,
                (password_hash,),
            ).fetchone()["id"]
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (vault_id, user_id),
            )
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=vault_id,
                path="a.txt",
                file_type="regular",
                size=3,
                mtime_ns=1,
                observed_at="2026-07-22T10:00:00+00:00",
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, path, action, status, message,
                    message_key, message_params, requested_by, requested_at, updated_at
                ) VALUES (
                    %s,
                    (SELECT id FROM vault_files WHERE vault_id=%s LIMIT 1),
                    'a.txt', 'upload', 'completed', 'Upload verified',
                    'job.upload_verified', '{}', %s,
                    '2026-07-22T10:00:00+00:00', '2026-07-22T10:00:00+00:00'
                )
                """,
                (vault_id, vault_id, user_id),
            )
            self.user_id = user_id
            self.vault_id = vault_id

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))
        with SQLiteConnection(str(self.database_path)) as connection:
            raw = create_session(
                connection,
                user_id=self.user_id,
                ip="127.0.0.1",
                user_agent="test",
                auth_method="oidc",
            )
        self.client.cookies.set(self.settings.session_cookie_name, raw)

    def test_jobs_api_localizes_message_for_locale_cookie(self) -> None:
        self.client.cookies.set(LOCALE_COOKIE_NAME, "it")
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        item = payload["items"][0]
        self.assertEqual(item["message_key"], "job.upload_verified")
        self.assertEqual(item["message"], "Caricamento verificato")
        self.assertEqual(payload["groups"][0]["message"], "Caricamento verificato")


if __name__ == "__main__":
    unittest.main()
