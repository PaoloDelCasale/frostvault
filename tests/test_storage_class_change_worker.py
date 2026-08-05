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
            side_effect=[
                {
                    "StorageClass": "STANDARD",
                    "VersionId": "s3-v1",
                    "ContentLength": 12,
                    "ETag": '"etag"',
                },
                {
                    "StorageClass": "STANDARD_IA",
                    "VersionId": "s3-v2",
                    "ContentLength": 12,
                    "ETag": '"etag2"',
                },
            ]
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
            job = connection.execute(
                "SELECT status, message_key, archive_version_id FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            version = connection.execute(
                "SELECT storage_class, provider_version_id, id FROM archive_versions WHERE id=%s",
                (job["archive_version_id"],),
            ).fetchone()
        self.assertEqual(version["id"], self.version_id)
        self.assertEqual(version["storage_class"], "STANDARD_IA")
        self.assertEqual(version["provider_version_id"], "s3-v2")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["message_key"], "job.storage_class_completed")
        client.copy_object.assert_called_once()
        client.delete_object.assert_not_called()
        self.assertEqual(client.head_object.call_args_list[1].kwargs["VersionId"], "s3-v2")
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

    def _large_copy_fixture(self, *, destination: dict | None = None) -> tuple[Mock, int]:
        size = storage_module.S3_SINGLE_COPY_MAX_BYTES + 1
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET size=%s WHERE id=%s",
                (size, self.version_id),
            )
            connection.execute(
                "UPDATE jobs SET total_bytes=%s WHERE id=%s",
                (size, self.job_id),
            )
        client = Mock()
        client.head_object = Mock(
            side_effect=[
                {
                    "StorageClass": "STANDARD",
                    "VersionId": "s3-v1",
                    "ContentLength": size,
                    "ETag": '"etag"',
                },
                destination
                or {
                    "StorageClass": "STANDARD_IA",
                    "VersionId": "s3-v2",
                    "ContentLength": size,
                    "ETag": '"multipart-etag"',
                },
            ]
        )
        client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
        client.upload_part_copy.side_effect = lambda **_: {
            "CopyPartResult": {"ETag": '"part-etag"'}
        }
        client.complete_multipart_upload.return_value = {"VersionId": "s3-v2"}
        client.abort_multipart_upload = Mock()
        return client, size

    def test_oversized_change_uses_exact_version_multipart_copy(self) -> None:
        client, size = self._large_copy_fixture()
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
            storage_module.process_job(dict(self._job_row()))

        self.assertGreater(client.upload_part_copy.call_count, 1)
        self.assertEqual(client.upload_part_copy.call_count, 41)
        self.assertEqual(client.complete_multipart_upload.call_count, 1)
        client.copy_object.assert_not_called()
        client.abort_multipart_upload.assert_not_called()
        for call in client.upload_part_copy.call_args_list:
            self.assertEqual(call.kwargs["CopySource"]["VersionId"], "s3-v1")
            self.assertEqual(call.kwargs["CopySource"]["Key"], "docs/report.txt")
        first = client.upload_part_copy.call_args_list[0].kwargs["CopySourceRange"]
        last = client.upload_part_copy.call_args_list[-1].kwargs["CopySourceRange"]
        self.assertEqual(first, f"bytes=0-{128 * 1024 * 1024 - 1}")
        self.assertEqual(last, f"bytes={40 * 128 * 1024 * 1024}-{size - 1}")

    def test_multipart_copy_aborts_after_part_failure(self) -> None:
        client, _size = self._large_copy_fixture()
        client.upload_part_copy.side_effect = RuntimeError("copy part failed")
        client.abort_multipart_upload.side_effect = RuntimeError("abort failed")
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
            storage_module.process_job(dict(self._job_row()))

        client.abort_multipart_upload.assert_called_once_with(
            Bucket="bucket", Key="docs/report.txt", UploadId="upload-1"
        )
        client.complete_multipart_upload.assert_not_called()
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status, message FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            version = connection.execute(
                "SELECT provider_version_id, storage_class FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIn("abort", (job["message"] or "").lower())
        self.assertEqual(version["provider_version_id"], "s3-v1")
        self.assertEqual(version["storage_class"], "STANDARD")
        client.delete_object.assert_not_called()

    def test_multipart_copy_aborts_when_cancelled_between_parts(self) -> None:
        client, _size = self._large_copy_fixture()

        def cancel_after_first_part(**_: object) -> dict:
            storage_module.cancel_jobs([self.job_id])
            return {"CopyPartResult": {"ETag": '"part-etag"'}}

        client.upload_part_copy.side_effect = cancel_after_first_part
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(self.path),
        )
        try:
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", database_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                storage_module.process_job(dict(self._job_row()))
        finally:
            with storage_module.operation_process_lock:
                storage_module.cancelled_jobs.discard(self.job_id)

        client.abort_multipart_upload.assert_called_once()
        client.complete_multipart_upload.assert_not_called()
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
        self.assertEqual(job["status"], "cancelled")
        client.delete_object.assert_not_called()

    def test_readback_mismatch_does_not_publish_storage_class(self) -> None:
        client, _size = self._large_copy_fixture(
            destination={
                "StorageClass": "STANDARD",
                "VersionId": "s3-v2",
                "ContentLength": 12,
            }
        )
        # Use the small path for a focused destination verification failure.
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE archive_versions SET size=12 WHERE id=%s",
                (self.version_id,),
            )
            connection.execute(
                "UPDATE jobs SET total_bytes=12 WHERE id=%s",
                (self.job_id,),
            )
        client.head_object.side_effect = [
            {
                "StorageClass": "STANDARD",
                "VersionId": "s3-v1",
                "ContentLength": 12,
                "ETag": '"etag"',
            },
            {
                "StorageClass": "STANDARD",
                "VersionId": "s3-v2",
                "ContentLength": 12,
            },
        ]
        client.create_multipart_upload.reset_mock()
        client.upload_part_copy.reset_mock()
        client.complete_multipart_upload.reset_mock()
        client.copy_object = Mock(
            return_value={"VersionId": "s3-v2", "CopyObjectResult": {"ETag": '"etag2"'}}
        )
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
            storage_module.process_job(dict(self._job_row()))

        client.copy_object.assert_called_once()
        client.delete_object.assert_not_called()
        with SQLiteConnection(str(self.path)) as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id=%s", (self.job_id,)
            ).fetchone()
            version = connection.execute(
                "SELECT provider_version_id, storage_class FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(version["provider_version_id"], "s3-v1")
        self.assertEqual(version["storage_class"], "STANDARD")

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
            side_effect=[
                {
                    "StorageClass": "DEEP_ARCHIVE",
                    "VersionId": "s3-v1",
                    "ContentLength": 12,
                    "Restore": (
                        'ongoing-request="false", '
                        'expiry-date="Wed, 01 Apr 2026 00:00:00 GMT"'
                    ),
                },
                {
                    "StorageClass": "STANDARD",
                    "VersionId": "s3-v2",
                    "ContentLength": 12,
                    "ETag": '"etag2"',
                },
            ]
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
            job = connection.execute(
                "SELECT status, message_key, archive_version_id FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
            version = connection.execute(
                """
                SELECT storage_class, provider_version_id, restore_state
                FROM archive_versions WHERE id=%s
                """,
                (job["archive_version_id"],),
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
        client.delete_object.assert_not_called()
        self.assertEqual(client.copy_object.call_args.kwargs["StorageClass"], "STANDARD")


if __name__ == "__main__":
    unittest.main()
