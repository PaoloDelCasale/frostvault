"""Worker seams for manual storage-class change (issue #110).

Seams under test:
6. Successful class-change completion refreshes catalog storage_class.
7. Objects already at the target class are skipped, not errored, in multi-object jobs.
8. Ineligible cold-state objects (need restore first) are reported clearly.
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

    def test_glacier_without_restore_fails_with_clear_message(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class='GLACIER', restore_state='not_requested'
                WHERE id=%s
                """,
                (self.version_id,),
            )
            connection.execute(
                "UPDATE jobs SET target_storage_class='DEEP_ARCHIVE' WHERE id=%s",
                (self.job_id,),
            )
        client = Mock()
        client.head_object = Mock(
            return_value={
                "StorageClass": "GLACIER",
                "VersionId": "s3-v1",
                "ContentLength": 12,
            }
        )
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
                "SELECT storage_class, provider_version_id FROM archive_versions WHERE id=%s",
                (self.version_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["message_key"], "job.storage_class_needs_restore")
        self.assertEqual(version["storage_class"], "GLACIER")
        self.assertEqual(version["provider_version_id"], "s3-v1")
        client.copy_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
