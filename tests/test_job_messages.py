"""Job messages persist as stable keys and localize at the jobs API seam.

Seams under test:
- ``storage.set_job`` / ``schedule_upload_retry`` persist ``message_key`` + params
- ``app.i18n.present_job_message`` / ``/api/vaults/{vault_id}/jobs`` localize for the request locale
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.catalog import ArchiveCatalog

from app.i18n import LOCALE_COOKIE_NAME, present_job_message
from app.storage import schedule_upload_retry, set_job
from tests.fixtures import FrostVaultTestCase, seed_vault


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


class JobMessagePersistenceTests(FrostVaultTestCase):
    owner = None

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            seed_vault(connection, id=2, source_root="/source")
            file_id = ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path="a.txt",
                file_type="regular",
                size=3,
                mtime_ns=1,
                observed_at="2026-07-22T10:00:00+00:00",
            )
            self.job_id = connection.execute(
                """
                INSERT INTO jobs(vault_id, file_id, action, status, requested_at, updated_at)
                VALUES (2, %s, 'upload', 'queued',
                        '2026-07-22T10:00:00+00:00', '2026-07-22T10:00:00+00:00')
                RETURNING id
                """,
                (file_id,),
            ).fetchone()["id"]

    def test_set_job_stores_message_key_and_english_fallback(self) -> None:
        set_job(
            self.job_id,
            "completed",
            message_key="job.upload_verified",
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, message, message_key, message_params FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["message_key"], "job.upload_verified")
        self.assertEqual(row["message"], "Upload verified")
        # ``message_params`` is jsonb (#589). Terminal notifications are
        # ported with the notification tables (#586).
        self.assertEqual(row["message_params"], {})

    def test_set_job_keeps_terminal_state_when_notification_sql_fails(self) -> None:
        def fail_notification(connection, *, job_id):
            del job_id
            connection.execute("INSERT INTO missing_notification_table(id) VALUES (1)")

        with patch(
            "app.storage.notification_service.enqueue_job_terminal_notification",
            side_effect=fail_notification,
        ):
            set_job(self.job_id, "completed", message_key="job.upload_verified")

        with self.database.connect() as connection:
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
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, message, message_key, message_params FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["message_key"], "job.retrying_transient")
        self.assertEqual(row["message"], "Retrying after transient error: SlowDown")
        self.assertEqual(row["message_params"], {"error": "SlowDown"})


class JobHttpFixture(FrostVaultTestCase):
    """The signed-in Owner with one Vault and one completed upload, and no tests."""

    PASSWORD = "correct horse battery staple"
    owner = {"password": PASSWORD}

    def setUp(self) -> None:
        super().setUp()
        with self.database.connect() as connection:
            vault_id = seed_vault(connection, name="Docs", source_root="/source")["id"]
            file_id = ArchiveCatalog(connection).observe_local_copy(
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
                    vault_id, file_id, action, status, message, message_key,
                    message_params, requested_at, updated_at
                ) VALUES (
                    %s, %s, 'upload', 'completed', 'Upload verified',
                    'job.upload_verified', '{}',
                    '2026-07-22T10:00:00+00:00', '2026-07-22T10:00:00+00:00'
                )
                """,
                (vault_id, file_id),
            )
            self.vault_id = vault_id
            self.file_id = file_id

        self.client = self.http_client()
        self.sign_in()


class JobMessageHttpTests(JobHttpFixture):
    def test_jobs_api_localizes_message_for_locale_cookie(self) -> None:
        self.client.cookies.set(LOCALE_COOKIE_NAME, "it")
        response = self.client.get(f"/api/vaults/{self.vault_id}/jobs")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        item = payload["items"][0]
        self.assertEqual(item["message_key"], "job.upload_verified")
        self.assertEqual(item["message"], "Caricamento verificato")
        self.assertEqual(payload["groups"][0]["message"], "Caricamento verificato")


if __name__ == "__main__":
    unittest.main()
