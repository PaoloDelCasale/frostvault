"""Worker seams for manual storage-class change (issue #110 / warm-restore).

Seams under test:
6. Successful class-change completion refreshes catalog storage_class.
1. Unrestored GLACIER/DEEP_ARCHIVE storage-class Job calls RestoreObject and
   enters restoring (does not fail with needs_restore).
2. When restore is available, Job copies to target class and updates catalog
   even if a Local Copy is already present.
3. process_job accepts storage-class while status=restoring.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app import storage as storage_module
from tests.test_database import run_alembic


class StorageClassChangeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        storage_module.cancelled_jobs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(storage_module.cancelled_jobs.clear)
        self.path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'x', FALSE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                """
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=1,
                path="report.txt",
                file_type="regular",
                size=12,
                mtime_ns=1,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            self.version_id = catalog.record_archive_version(
                vault_id=1,
                path="report.txt",
                object_key="docs/report.txt",
                provider_version_id="s3-v1",
                size=12,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="scan",
                origin="upload",
            )
            catalog.mark_version_verified(
                self.version_id,
                plaintext_sha256="a" * 64,
                verified_at="2026-07-21T10:01:00+00:00",
            )
            file_row = catalog.get_file_by_path(1, "report.txt")
            self.vault_file_id = file_row["id"]
            self.job_id = connection.execute(
                """
                INSERT INTO jobs(
                    vault_id, vault_file_id, archive_version_id, path,
                    action, status, requested_by, requested_at, updated_at,
                    group_id, group_path, total_bytes, transferred_bytes,
                    target_storage_class, origin
                ) VALUES (
                    1, %s, %s, 'report.txt',
                    'storage-class', 'queued', 1, '2026-07-21T12:00:00+00:00',
                    '2026-07-21T12:00:00+00:00',
                    'g1', 'report.txt', 12, 0,
                    'STANDARD_IA', 'manual'
                )
                RETURNING id
                """,
                (self.vault_file_id, self.version_id),
            ).fetchone()["id"]

    def _job_row(self) -> dict:
        with SQLiteConnection(str(self.path)) as connection:
            return connection.execute(
                """
                SELECT j.*, v.encryption_mode, v.s3_bucket, v.s3_prefix, v.source_root,
                       v.rclone_remote, v.slug
                FROM jobs j
                JOIN vaults v ON v.id=j.vault_id
                WHERE j.id=%s
                """,
                (self.job_id,),
            ).fetchone()

    def test_successful_change_updates_catalog_storage_class_and_version_id(self) -> None:
        client = Mock()
        client.head_object = Mock(
            return_value={
                "StorageClass": "STANDARD",
                "VersionId": "s3-v1",
                "ContentLength": 12,
                "ETag": '"etag"',
            }
        )
        client.copy_object = Mock(
            return_value={"VersionId": "s3-v2", "CopyObjectResult": {"ETag": '"etag2"'}}
        )
        client.delete_object = Mock(return_value={})
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.path),
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.settings", database_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=client),
        ):
            job = dict(self._job_row())
            storage_module.process_job(job)

        with SQLiteConnection(str(self.path)) as connection:
            version = connection.execute(
                "SELECT storage_class, provider_version_id, id FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT status, message_key FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(version["id"], self.version_id)
        self.assertEqual(version["storage_class"], "STANDARD_IA")
        self.assertEqual(version["provider_version_id"], "s3-v2")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["message_key"], "job.storage_class_completed")
        client.copy_object.assert_called_once()
        copy_kwargs = client.copy_object.call_args.kwargs
        self.assertEqual(copy_kwargs["StorageClass"], "STANDARD_IA")
        self.assertEqual(copy_kwargs["Key"], "docs/report.txt")

    def test_restart_completes_catalogued_storage_class_destination_after_provider_check(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            ArchiveCatalog(connection).update_version_storage_placement(
                self.version_id,
                provider_version_id="s3-v2",
                storage_class="STANDARD_IA",
                etag="etag2",
                observed_at="2026-07-21T12:01:00+00:00",
            )
            connection.execute(
                """
                UPDATE jobs
                SET status='uploading', claim_token='dead-worker',
                    claimed_at='2026-07-21T12:01:00+00:00',
                    claim_expires_at='2000-01-01T00:00:00+00:00'
                WHERE id=%s
                """,
                (self.job_id,),
            )
        client = Mock()
        client.head_object.return_value = {
            "VersionId": "s3-v2",
            "StorageClass": "STANDARD_IA",
            "ContentLength": 12,
            "ETag": '"etag2"',
        }
        database_settings = SimpleNamespace(
            db_backend="sqlite", sqlite_path=str(self.path)
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.s3_client", return_value=client),
        ):
            summary = storage_module.reconcile_interrupted_jobs()

        self.assertEqual(summary, {"completed": 1, "requeued": 0, "failed": 0})
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, claim_token FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["claim_token"])
        client.copy_object.assert_not_called()

    def test_restart_after_copy_before_catalog_fails_closed_without_second_copy(self) -> None:
        """Issue #193: an unrecorded CopyObject result is never blindly retried."""
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status='uploading', claim_token='dead-worker',
                    claimed_at='2026-07-21T12:01:00+00:00',
                    claim_expires_at='2000-01-01T00:00:00+00:00'
                WHERE id=%s
                """,
                (self.job_id,),
            )
        client = Mock()
        # This is exactly the crash window: S3 made a new current target
        # VersionId, but the catalog still pins s3-v1/STANDARD.
        client.head_object.return_value = {
            "VersionId": "s3-v2",
            "StorageClass": "STANDARD_IA",
            "ContentLength": 12,
            "ETag": '"etag"',
        }
        database_settings = SimpleNamespace(
            db_backend="sqlite", sqlite_path=str(self.path)
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.s3_client", return_value=client),
        ):
            summary = storage_module.reconcile_interrupted_jobs()

        self.assertEqual(summary, {"completed": 0, "requeued": 0, "failed": 1})
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, message FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            version = connection.execute(
                """
                SELECT provider_version_id, storage_class
                FROM archive_versions WHERE id=%s
                """,
                (self.version_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIn("manual review", (job["message"] or "").lower())
        self.assertEqual(version["provider_version_id"], "s3-v1")
        self.assertEqual(version["storage_class"], "STANDARD")
        client.copy_object.assert_not_called()

    def test_unrestored_deep_archive_requests_restore_and_enters_restoring(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class='DEEP_ARCHIVE', restore_state='not_requested'
                WHERE id=%s
                """,
                (self.version_id,),
            )
            connection.execute(
                """
                UPDATE jobs
                SET target_storage_class='STANDARD',
                    restore_tier='Bulk',
                    restore_days=7
                WHERE id=%s
                """,
                (self.job_id,),
            )
        client = Mock()
        client.head_object = Mock(
            return_value={
                "StorageClass": "DEEP_ARCHIVE",
                "VersionId": "s3-v1",
                "ContentLength": 12,
            }
        )
        client.restore_object = Mock(return_value={})
        client.copy_object = Mock()
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.path),
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.settings", database_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=client),
        ):
            job = dict(self._job_row())
            storage_module.process_job(job)

        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, message_key FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            version = connection.execute(
                """
                SELECT storage_class, provider_version_id, restore_state
                FROM archive_versions WHERE id=%s
                """,
                (self.version_id,),
            ).fetchone()
        self.assertEqual(job["status"], "restoring")
        self.assertEqual(job["message_key"], "job.storage_class_restoring")
        self.assertEqual(version["storage_class"], "DEEP_ARCHIVE")
        self.assertEqual(version["restore_state"], "restoring")
        client.restore_object.assert_called_once()
        restore_kwargs = client.restore_object.call_args.kwargs
        self.assertEqual(restore_kwargs["VersionId"], "s3-v1")
        self.assertEqual(
            restore_kwargs["RestoreRequest"]["GlacierJobParameters"]["Tier"],
            "Bulk",
        )
        client.copy_object.assert_not_called()

    def test_restored_deep_archive_warms_to_standard_with_local_copy_present(
        self,
    ) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class='DEEP_ARCHIVE', restore_state='available'
                WHERE id=%s
                """,
                (self.version_id,),
            )
            connection.execute(
                "UPDATE jobs SET target_storage_class='STANDARD', status='restoring' WHERE id=%s",
                (self.job_id,),
            )
        client = Mock()
        client.head_object = Mock(
            return_value={
                "StorageClass": "DEEP_ARCHIVE",
                "VersionId": "s3-v1",
                "ContentLength": 12,
                "Restore": (
                    'ongoing-request="false", '
                    'expiry-date="Wed, 01 Apr 2026 00:00:00 GMT"'
                ),
            }
        )
        client.copy_object = Mock(
            return_value={"VersionId": "s3-v2", "CopyObjectResult": {"ETag": '"etag2"'}}
        )
        client.delete_object = Mock(return_value={})
        client.restore_object = Mock()
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.path),
            restore_poll_interval=0,
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.settings", database_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.s3_client", return_value=client),
        ):
            job = dict(self._job_row())
            processed = storage_module.process_job(job)

        self.assertTrue(processed)
        with SQLiteConnection(str(self.path)) as connection:
            version = connection.execute(
                """
                SELECT storage_class, provider_version_id, restore_state
                FROM archive_versions WHERE id=%s
                """,
                (self.version_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT status, message_key FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            local = connection.execute(
                "SELECT presence FROM local_copies WHERE vault_file_id=%s",
                (self.vault_file_id,),
            ).fetchone()
        self.assertEqual(local["presence"], "present")
        self.assertEqual(version["storage_class"], "STANDARD")
        self.assertEqual(version["provider_version_id"], "s3-v2")
        self.assertIsNone(version["restore_state"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["message_key"], "job.storage_class_completed")
        client.restore_object.assert_not_called()
        client.copy_object.assert_called_once()
        self.assertEqual(client.copy_object.call_args.kwargs["StorageClass"], "STANDARD")


if __name__ == "__main__":
    unittest.main()
