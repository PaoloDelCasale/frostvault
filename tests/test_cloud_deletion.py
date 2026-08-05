"""Reversible archival and permanent cloud purge (issue #10).

Seams under test:
- `app.services.cloud_deletion` public helpers: vault deletion setting,
  selection preview, confirmation phrase checks, delete-marker copy,
  schedule/cancel gates for reversible archive and permanent purge.
- Worker execution via `process_jobs_once` / `process_cloud_archive` /
  `process_cloud_purge`: S3 delete_object (marker only) vs
  DeleteObjectVersion for every version/marker; cancel before delay
  must never call S3; batch items resume with per-version failures;
  post-marker notify failures must not fail-close archive (BUG-017).
- Lifecycle rule builder emits NoncurrentVersionTransitions for retained
  noncurrent Archive Versions (cheapest configured archive class).

System boundaries mocked: S3 client. Catalog and jobs use a real SQLite
schema via Alembic.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services import cloud_deletion
from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.s3_lifecycle_rules import build_policy_lifecycle_rule
from app.storage import process_jobs_once
from app import storage as storage_module
from tests.test_database import run_alembic


def _now() -> datetime:
    return datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


class _CloudDeletionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        storage_module.cancelled_jobs.clear()
        self.addCleanup(storage_module.cancelled_jobs.clear)


def _prepare_vault_with_versions(
    root: Path,
    *,
    cloud_deletion_enabled: bool = False,
    version_count: int = 2,
    revision: str = "head",
) -> tuple[Path, int, str]:
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path, revision)
    assert migrated.returncode == 0, migrated.stderr
    source = root / "source"
    source.mkdir()
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, display_name, password_hash, is_admin)
            VALUES (1, 'owner', 'Owner', 'hash', TRUE)
            """
        )
        connection.execute(
            """
            INSERT INTO vaults(
                id, slug, name, source_root, s3_bucket, s3_prefix,
                rclone_remote, cloud_deletion_enabled
            ) VALUES (
                2, 'docs', 'Docs Archive', %s, 'bucket', 'docs', 'remote', %s
            )
            """,
            (str(source), cloud_deletion_enabled),
        )
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=12,
            mtime_ns=1_700_000_000_000_000_000,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        file_row = catalog.get_file_by_path(2, "report.txt")
        version_ids: list[str] = []
        for index in range(version_count):
            version_ids.append(
                catalog.record_archive_version(
                    vault_id=2,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id=f"s3-v{index + 1}",
                    size=10 + index,
                    storage_class="STANDARD",
                    etag=f"etag-{index}",
                    uploaded_at=f"2026-07-21T10:0{index}:00+00:00",
                    observed_at=f"2026-07-21T10:0{index}:00+00:00",
                    scan_id="scan-1",
                    origin="upload",
                )
            )
            catalog.mark_version_verified(
                version_ids[-1],
                plaintext_sha256="a" * 64,
                verified_at=f"2026-07-21T10:0{index}:30+00:00",
            )
        catalog.mark_local_copy_missing(
            file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
        )
        return database_path, 2, file_row["id"]


class CloudDeletionSettingTests(_CloudDeletionTestCase):
    def test_vault_cloud_deletion_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "catalog.db"
            self.assertEqual(run_alembic(database_path).returncode, 0)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/src', 'b', 'p', 'r')
                    """
                )
                enabled = cloud_deletion.is_cloud_deletion_enabled(connection, 1)
            self.assertFalse(enabled)

    def test_owner_can_enable_cloud_deletion_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "catalog.db"
            self.assertEqual(run_alembic(database_path).returncode, 0)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, display_name, password_hash, is_admin)
                    VALUES (9, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                    ) VALUES (1, 'docs', 'Docs', '/src', 'b', 'p', 'r')
                    """
                )
                cloud_deletion.set_cloud_deletion_enabled(
                    connection, vault_id=1, enabled=True, actor_user_id=9
                )
                self.assertTrue(cloud_deletion.is_cloud_deletion_enabled(connection, 1))


class DeletionPreviewTests(_CloudDeletionTestCase):
    def test_preview_counts_objects_versions_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                preview = cloud_deletion.preview_selection(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                )
            self.assertEqual(preview.object_count, 1)
            self.assertEqual(preview.version_count, 2)
            self.assertEqual(preview.byte_count, 21)
            self.assertEqual(preview.delete_marker_count, 0)

    def test_directory_preview_and_archive_select_nested_files_only(self) -> None:
        """Folder selection uses path prefix matching without touching siblings."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "catalog.db"
            self.assertEqual(run_alembic(database_path).returncode, 0)
            source = root / "source"
            source.mkdir()
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, display_name, password_hash, is_admin)
                    VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote, cloud_deletion_enabled
                    ) VALUES (
                        2, 'docs', 'Docs Archive', %s, 'bucket', 'docs', 'remote', TRUE
                    )
                    """,
                    (str(source),),
                )
                catalog = ArchiveCatalog(connection)
                for path, key in (
                    ("reports/a.txt", "docs/reports/a.txt"),
                    ("reports/nested/b.txt", "docs/reports/nested/b.txt"),
                    ("sibling.txt", "docs/sibling.txt"),
                ):
                    catalog.observe_local_copy(
                        vault_id=2,
                        path=path,
                        file_type="regular",
                        size=10,
                        mtime_ns=1_700_000_000_000_000_000,
                        observed_at="2026-07-21T10:00:00+00:00",
                    )
                    version_id = catalog.record_archive_version(
                        vault_id=2,
                        path=path,
                        object_key=key,
                        provider_version_id=f"s3-{path}",
                        size=10,
                        storage_class="STANDARD",
                        etag=f"etag-{path}",
                        uploaded_at="2026-07-21T10:00:00+00:00",
                        observed_at="2026-07-21T10:00:00+00:00",
                        scan_id="scan-1",
                        origin="upload",
                    )
                    catalog.mark_version_verified(
                        version_id,
                        plaintext_sha256="b" * 64,
                        verified_at="2026-07-21T10:00:30+00:00",
                    )
                preview = cloud_deletion.preview_selection(
                    connection,
                    vault_id=2,
                    paths=["reports"],
                    is_directory=True,
                )
                self.assertEqual(preview.object_count, 2)
                scheduled = cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=2,
                    paths=["reports"],
                    is_directory=True,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                )
                paths = {
                    row["path"]
                    for row in connection.execute(
                        "SELECT path FROM jobs WHERE group_id=%s",
                        (scheduled.group_id,),
                    ).fetchall()
                }
            self.assertEqual(paths, {"reports/a.txt", "reports/nested/b.txt"})
            self.assertNotIn("sibling.txt", paths)


class ConfirmationPhraseTests(_CloudDeletionTestCase):
    def test_vault_name_or_generated_phrase_is_accepted(self) -> None:
        self.assertTrue(
            cloud_deletion.confirmation_matches(
                provided="Docs Archive",
                vault_name="Docs Archive",
                generated_phrase="purple-orchid-42",
            )
        )
        self.assertTrue(
            cloud_deletion.confirmation_matches(
                provided="purple-orchid-42",
                vault_name="Docs Archive",
                generated_phrase="purple-orchid-42",
            )
        )
        self.assertFalse(
            cloud_deletion.confirmation_matches(
                provided="wrong",
                vault_name="Docs Archive",
                generated_phrase="purple-orchid-42",
            )
        )

    def test_delete_marker_copy_never_claims_object_data(self) -> None:
        copy = cloud_deletion.delete_marker_explanation()
        lowered = copy.lower()
        self.assertIn("delete marker", lowered)
        self.assertNotIn("contains object data", lowered)
        self.assertNotIn("transitioning object data", lowered)
        self.assertNotIn("stores data", lowered)


class ReversibleArchiveGateTests(_CloudDeletionTestCase):
    def test_archive_requires_enabled_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=False
            )
            with SQLiteConnection(str(database_path)) as connection:
                with self.assertRaises(cloud_deletion.CloudDeletionDisabled):
                    cloud_deletion.schedule_cloud_archive(
                        connection,
                        vault_id=vault_id,
                        paths=["report.txt"],
                        is_directory=False,
                        actor_user_id=1,
                        requested_at=_iso(_now()),
                    )


class PermanentPurgeGateTests(_CloudDeletionTestCase):
    def test_purge_does_not_schedule_vault_files_without_cloud_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _ = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=0
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=vault_id,
                    path="cloud.txt",
                    file_type="regular",
                    size=10,
                    mtime_ns=1_700_000_000_000_000_001,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=vault_id,
                    path="cloud.txt",
                    object_key="docs/cloud.txt",
                    provider_version_id="cloud-v1",
                    size=10,
                    storage_class="STANDARD",
                    etag="etag-cloud",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan-1",
                    origin="upload",
                )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt", "cloud.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=60,
                )
                jobs = connection.execute(
                    "SELECT path FROM jobs WHERE group_id=%s ORDER BY path",
                    (scheduled.group_id,),
                ).fetchall()
            self.assertEqual([job["path"] for job in jobs], ["cloud.txt"])

    def test_purge_group_total_bytes_matches_selection_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=vault_id,
                    path="appendix.txt",
                    file_type="regular",
                    size=5,
                    mtime_ns=1_700_000_000_000_000_000,
                    observed_at="2026-07-21T10:10:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=vault_id,
                    path="appendix.txt",
                    object_key="docs/appendix.txt",
                    provider_version_id="s3-appendix",
                    size=5,
                    storage_class="STANDARD",
                    etag="etag-appendix",
                    uploaded_at="2026-07-21T10:10:00+00:00",
                    observed_at="2026-07-21T10:10:00+00:00",
                    scan_id="scan-1",
                    origin="upload",
                )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt", "appendix.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="obsolete duplicates",
                    generated_phrase="purple-orchid-42",
                    delay_seconds=86400,
                )
                rows = connection.execute(
                    """
                    SELECT id, path, action, status, message, message_key,
                           requested_at, updated_at, group_id, group_path,
                           total_bytes, transferred_bytes, pending_until,
                           estimated_cost_eur, estimated_hours, restore_tier,
                           restore_days
                    FROM jobs WHERE group_id=%s
                    """,
                    (scheduled.group_id,),
                ).fetchall()

            from app.main import build_job_groups

            group = build_job_groups(rows)[0]
            self.assertEqual(scheduled.preview.byte_count, 26)
            self.assertEqual(group["total_bytes"], scheduled.preview.byte_count)

    def test_migration_repairs_existing_purge_job_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory),
                cloud_deletion_enabled=True,
                revision="0026_invite_revocation",
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=vault_id,
                    path="appendix.txt",
                    file_type="regular",
                    size=5,
                    mtime_ns=1_700_000_000_000_000_000,
                    observed_at="2026-07-21T10:10:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=vault_id,
                    path="appendix.txt",
                    object_key="docs/appendix.txt",
                    provider_version_id="s3-appendix",
                    size=5,
                    storage_class="STANDARD",
                    etag="etag-appendix",
                    uploaded_at="2026-07-21T10:10:00+00:00",
                    observed_at="2026-07-21T10:10:00+00:00",
                    scan_id="scan-1",
                    origin="upload",
                )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt", "appendix.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="obsolete duplicates",
                    generated_phrase="purple-orchid-42",
                    delay_seconds=86400,
                )
                connection.execute(
                    "UPDATE jobs SET total_bytes=%s WHERE group_id=%s",
                    (scheduled.preview.byte_count, scheduled.group_id),
                )

            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                total = connection.execute(
                    "SELECT SUM(total_bytes) AS total FROM jobs WHERE group_id=%s",
                    (scheduled.group_id,),
                ).fetchone()["total"]

            self.assertEqual(total, scheduled.preview.byte_count)

    def test_purge_requires_setting_confirmation_reason_and_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True
            )
            with SQLiteConnection(str(database_path)) as connection:
                with self.assertRaises(cloud_deletion.ConfirmationRequired):
                    cloud_deletion.schedule_cloud_purge(
                        connection,
                        vault_id=vault_id,
                        paths=["report.txt"],
                        is_directory=False,
                        actor_user_id=1,
                        requested_at=_iso(_now()),
                        confirmation="",
                        reason="cleanup",
                        generated_phrase="purple-orchid-42",
                        delay_seconds=86400,
                    )
                with self.assertRaises(cloud_deletion.ReasonRequired):
                    cloud_deletion.schedule_cloud_purge(
                        connection,
                        vault_id=vault_id,
                        paths=["report.txt"],
                        is_directory=False,
                        actor_user_id=1,
                        requested_at=_iso(_now()),
                        confirmation="Docs Archive",
                        reason="  ",
                        generated_phrase="purple-orchid-42",
                        delay_seconds=86400,
                    )
                result = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="obsolete duplicates",
                    generated_phrase="purple-orchid-42",
                    delay_seconds=86400,
                )
                job = connection.execute(
                    "SELECT status, pending_until, reason, action FROM jobs WHERE id=%s",
                    (result.job_ids[0],),
                ).fetchone()
            self.assertEqual(job["action"], "cloud-purge")
            self.assertEqual(job["status"], "pending_delay")
            self.assertEqual(job["reason"], "obsolete duplicates")
            self.assertIsNotNone(job["pending_until"])
            pending = datetime.fromisoformat(job["pending_until"])
            self.assertEqual(pending, _now() + timedelta(seconds=86400))

    def test_accelerate_skips_delay_and_queues_purge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True
            )
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="cleanup",
                    generated_phrase="x",
                    delay_seconds=86400,
                )
                accelerated_at = _iso(_now())
                accelerated = cloud_deletion.accelerate_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    group_id=scheduled.group_id,
                    actor_user_id=1,
                    accelerated_at=accelerated_at,
                )
                job = connection.execute(
                    """
                    SELECT status, pending_until, message_key
                    FROM jobs WHERE id=%s
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()
                with self.assertRaises(ValueError):
                    cloud_deletion.accelerate_cloud_purge(
                        connection,
                        vault_id=vault_id,
                        group_id=scheduled.group_id,
                        actor_user_id=1,
                        accelerated_at=accelerated_at,
                    )
            self.assertEqual(accelerated.accelerated_count, 1)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["pending_until"], accelerated_at)
            self.assertEqual(job["message_key"], "job.cloud_purge_accelerated")

    def test_cancel_during_delay_prevents_all_deletion_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True
            )
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                    confirmation="Docs Archive",
                    reason="cleanup",
                    generated_phrase="x",
                    delay_seconds=86400,
                )
                cancelled = cloud_deletion.cancel_cloud_deletion(
                    connection,
                    vault_id=vault_id,
                    group_id=scheduled.group_id,
                    actor_user_id=1,
                    cancelled_at=_iso(_now()),
                )
            self.assertEqual(cancelled.cancelled_count, 1)

            client = Mock()
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                cloud_purge_delay_seconds=86400,
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()
            client.delete_object.assert_not_called()
            if hasattr(client, "delete_object_versions"):
                client.delete_object_versions.assert_not_called()


class ReversibleArchiveExecutionTests(_CloudDeletionTestCase):
    def test_reversible_archive_creates_delete_marker_without_purging_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                )

            client = Mock()
            client.head_object.return_value = {
                "VersionId": "s3-v2",
                "ContentLength": 11,
            }
            client.delete_object.return_value = {"VersionId": "marker-1", "DeleteMarker": True}
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.head_object.assert_called_once_with(
                Bucket="bucket", Key="docs/report.txt"
            )
            client.delete_object.assert_called_once()
            kwargs = client.delete_object.call_args.kwargs
            self.assertEqual(kwargs["Key"], "docs/report.txt")
            self.assertNotIn("VersionId", kwargs)

            with SQLiteConnection(str(database_path)) as connection:
                markers = connection.execute(
                    "SELECT provider_version_id FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
                versions = connection.execute(
                    """
                    SELECT availability, provider_version_id
                    FROM archive_versions WHERE vault_file_id=%s
                    ORDER BY version_number
                    """,
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status FROM jobs WHERE action='cloud-archive'"
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual([row["provider_version_id"] for row in markers], ["marker-1"])
            self.assertEqual(
                [row["availability"] for row in versions],
                ["available", "available"],
            )

    def test_bug_002_cloud_archive_detects_concurrent_version(self) -> None:
        """[BUG-002][Req: REQ-004] compare live current VersionId before hide.

        Seam: process_jobs_once → process_cloud_archive with S3 mocked at the
        system boundary (same path as reversible-archive execution tests).

        Schedules cloud-archive against the current Archive Version (s3-v2),
        presents a newer live VersionId via Head, and asserts the hide aborts
        instead of creating a Delete Marker on the wrong generation.
        """
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                )
                scheduled = connection.execute(
                    """
                    SELECT archive_version_id FROM jobs
                    WHERE action='cloud-archive'
                    """
                ).fetchone()
                pinned = connection.execute(
                    """
                    SELECT provider_version_id FROM archive_versions
                    WHERE id=%s
                    """,
                    (scheduled["archive_version_id"],),
                ).fetchone()["provider_version_id"]

            self.assertEqual(pinned, "s3-v2")

            client = Mock()
            client.head_object.return_value = {
                "VersionId": "s3-v-concurrent",
                "ContentLength": 12,
            }
            client.delete_object.return_value = {
                "VersionId": "marker-wrong",
                "DeleteMarker": True,
            }
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.delete_object.assert_not_called()
            with SQLiteConnection(str(database_path)) as connection:
                markers = connection.execute(
                    "SELECT provider_version_id FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE action='cloud-archive'"
                ).fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertIn("VersionId", job["message"] or "")
            self.assertEqual(markers, [])

    def test_bug_017_archive_observability_best_effort(self) -> None:
        """[BUG-017][Req: REQ-029] post-marker notify must not fail-close the job.

        Seam: process_jobs_once → process_cloud_archive → record_archive_completed
        with S3 mocked at the system boundary and enqueue_notification forced to
        raise after the Delete Marker is written to S3.

        A notify failure must leave the job completed with the Delete Marker
        catalogued (parity with local cleanup best-effort observability), so a
        later retry cannot issue another hide.
        """
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vault_members(vault_id, user_id, role)
                    VALUES (%s, 1, 'owner')
                    """,
                    (vault_id,),
                )
                cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                )

            client = Mock()
            client.head_object.return_value = {
                "VersionId": "s3-v2",
                "ContentLength": 11,
            }
            client.delete_object.return_value = {
                "VersionId": "marker-1",
                "DeleteMarker": True,
            }
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
                patch(
                    "app.services.cloud_deletion.enqueue_notification",
                    side_effect=RuntimeError("notify boom"),
                ),
            ):
                process_jobs_once()

            client.delete_object.assert_called_once()
            with SQLiteConnection(str(database_path)) as connection:
                markers = connection.execute(
                    "SELECT provider_version_id FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE action='cloud-archive'"
                ).fetchone()
            self.assertEqual(
                job["status"],
                "completed",
                "notify failure must not fail-close the archive job after Delete Marker",
            )
            self.assertEqual(
                [row["provider_version_id"] for row in markers],
                ["marker-1"],
                "Delete Marker catalog row must survive notify failure",
            )


class PermanentPurgeExecutionTests(_CloudDeletionTestCase):
    def test_purge_deletes_many_vault_files_in_one_s3_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _ = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=0
            )
            paths: list[str] = []
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                for index in range(10):
                    path = f"bulk/file-{index:02}.txt"
                    paths.append(path)
                    catalog.observe_local_copy(
                        vault_id=vault_id,
                        path=path,
                        file_type="regular",
                        size=10,
                        mtime_ns=1_700_000_000_000_000_000 + index,
                        observed_at="2026-07-21T10:00:00+00:00",
                    )
                    catalog.record_archive_version(
                        vault_id=vault_id,
                        path=path,
                        object_key=f"docs/{path}",
                        provider_version_id=f"version-{index}",
                        size=10,
                        storage_class="STANDARD",
                        etag=f"etag-{index}",
                        uploaded_at="2026-07-21T10:00:00+00:00",
                        observed_at="2026-07-21T10:00:00+00:00",
                        scan_id="scan-1",
                        origin="upload",
                    )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=paths,
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now() - timedelta(hours=25)),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=86400,
                )
                connection.execute(
                    "UPDATE jobs SET pending_until=%s WHERE group_id=%s",
                    ("2020-01-01T00:00:00+00:00", scheduled.group_id),
                )

            client = Mock()
            client.delete_objects.return_value = {}
            preflight = Mock()
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=16)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault", preflight),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            preflight.assert_called_once()
            client.delete_objects.assert_called_once()
            self.assertEqual(
                len(client.delete_objects.call_args.kwargs["Delete"]["Objects"]),
                10,
            )
            with SQLiteConnection(str(database_path)) as connection:
                statuses = connection.execute(
                    "SELECT status FROM jobs WHERE group_id=%s",
                    (scheduled.group_id,),
                ).fetchall()
            self.assertEqual({row["status"] for row in statuses}, {"completed"})

    def test_purge_deletes_many_versions_in_one_s3_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=100
            )
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now() - timedelta(hours=25)),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=86400,
                )
                connection.execute(
                    "UPDATE jobs SET pending_until=%s WHERE id=%s",
                    ("2020-01-01T00:00:00+00:00", scheduled.job_ids[0]),
                )

            client = Mock()
            client.delete_objects.return_value = {}
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.delete_objects.assert_called_once()
            self.assertEqual(
                len(client.delete_objects.call_args.kwargs["Delete"]["Objects"]),
                100,
            )
            client.delete_object.assert_not_called()
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s",
                    (scheduled.job_ids[0],),
                ).fetchone()
                remaining = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM archive_versions
                    WHERE vault_file_id=%s AND availability <> 'purged'
                    """,
                    (file_id,),
                ).fetchone()["total"]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(remaining, 0)

    def test_purge_after_delay_deletes_every_version_and_marker_resumably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                catalog.record_delete_marker(
                    vault_id=vault_id,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="marker-old",
                    created_at="2026-07-21T12:00:00+00:00",
                    observed_at="2026-07-21T12:00:00+00:00",
                )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now() - timedelta(hours=25)),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=86400,
                )
                # Simulate delay already elapsed relative to wall clock.
                connection.execute(
                    "UPDATE jobs SET pending_until=%s WHERE id=%s",
                    ("2020-01-01T00:00:00+00:00", scheduled.job_ids[0]),
                )

            client = Mock()
            deleted: list[dict] = []

            def delete_objects(**kwargs):
                deleted.extend(kwargs["Delete"]["Objects"])
                return {
                    "Errors": [
                        {
                            "Key": "docs/report.txt",
                            "VersionId": "s3-v2",
                            "Code": "InternalError",
                            "Message": "transient version delete failure",
                        }
                    ]
                }

            client.delete_objects = Mock(side_effect=delete_objects)
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            version_ids_called = {
                call.get("VersionId") for call in deleted if call.get("VersionId")
            }
            self.assertIn("s3-v1", version_ids_called)
            self.assertIn("marker-old", version_ids_called)

            with SQLiteConnection(str(database_path)) as connection:
                items = connection.execute(
                    """
                    SELECT kind, provider_version_id, status
                    FROM cloud_deletion_items
                    WHERE job_id=%s
                    ORDER BY id
                    """,
                    (scheduled.job_ids[0],),
                ).fetchall()
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s",
                    (scheduled.job_ids[0],),
                ).fetchone()
                versions = connection.execute(
                    "SELECT availability FROM archive_versions WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchall()
            statuses = {row["provider_version_id"]: row["status"] for row in items}
            self.assertEqual(statuses["s3-v1"], "deleted")
            self.assertEqual(statuses["marker-old"], "deleted")
            self.assertEqual(statuses["s3-v2"], "failed")
            self.assertEqual(job["status"], "failed")
            self.assertIn("purged", {row["availability"] for row in versions})

            # Resume: fix the failing version and rerun.
            client.delete_objects = Mock(return_value={})
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    "UPDATE jobs SET status='queued', message=NULL WHERE id=%s",
                    (scheduled.job_ids[0],),
                )
                connection.execute(
                    """
                    UPDATE cloud_deletion_items
                    SET status='pending', error_message=NULL
                    WHERE job_id=%s AND provider_version_id='s3-v2'
                    """,
                    (scheduled.job_ids[0],),
                )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                pending = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM cloud_deletion_items
                    WHERE job_id=%s AND status='pending'
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()["total"]
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s",
                    (scheduled.job_ids[0],),
                ).fetchone()
                vault_file = connection.execute(
                    "SELECT status FROM vault_files WHERE id=%s",
                    (file_id,),
                ).fetchone()
                path_history = connection.execute(
                    "SELECT COUNT(*) AS total FROM file_paths WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["total"]
            self.assertEqual(pending, 0)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(vault_file["status"], "purged")
            self.assertGreaterEqual(path_history, 1)


class ClaimLeaseSchedulerTests(_CloudDeletionTestCase):
    def test_two_schedulers_claim_a_cloud_purge_group_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=1
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                second_file = catalog.observe_local_copy(
                    vault_id=vault_id,
                    path="second.txt",
                    file_type="regular",
                    size=4,
                    mtime_ns=2,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                second_version = catalog.record_archive_version(
                    vault_id=vault_id,
                    path="second.txt",
                    object_key="docs/second.txt",
                    provider_version_id="second-version",
                    size=4,
                    storage_class="STANDARD",
                    etag="second-etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan",
                )
                catalog.mark_version_verified(
                    second_version,
                    plaintext_sha256="b" * 64,
                    verified_at="2026-07-21T10:01:00+00:00",
                )
                catalog.mark_local_copy_missing(
                    second_file, observed_at="2026-07-21T10:02:00+00:00"
                )
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt", "second.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now() - timedelta(hours=25)),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=60,
                )
                connection.execute(
                    "UPDATE jobs SET pending_until=%s WHERE group_id=%s",
                    ("2000-01-01T00:00:00+00:00", scheduled.group_id),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            runtime = SimpleNamespace(
                operation_concurrency=1,
                bandwidth_limit_kibps=None,
                restore_poll_interval=0,
            )
            start = threading.Barrier(2)
            processed: list[int] = []
            processed_lock = threading.Lock()

            def record(job: dict) -> bool:
                with processed_lock:
                    processed.append(int(job["id"]))
                return True

            def run_once(_: int) -> int:
                start.wait(timeout=10)
                return process_jobs_once()

            with (
                patch("app.database.settings", database_settings),
                patch("app.storage._runtime_settings", return_value=runtime),
                patch(
                    "app.storage.db",
                    side_effect=lambda: SQLiteConnection(str(database_path)),
                ),
                patch("app.storage.process_job", side_effect=record),
            ):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    results = list(workers.map(run_once, range(2)))

            self.assertEqual(sum(results), 1)
            self.assertEqual(len(processed), 1)
            with SQLiteConnection(str(database_path)) as connection:
                jobs = connection.execute(
                    """
                    SELECT status, claim_token
                    FROM jobs WHERE group_id=%s ORDER BY id
                    """,
                    (scheduled.group_id,),
                ).fetchall()
            self.assertEqual([job["status"] for job in jobs], ["cleaning", "cleaning"])
            self.assertEqual(len({job["claim_token"] for job in jobs}), 1)
            self.assertTrue(jobs[0]["claim_token"])


class ClaimLeaseTakeoverTests(_CloudDeletionTestCase):
    def test_takeover_after_delete_does_not_publish_stale_marker_or_repeat_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=1
            )
            timestamp = storage_module.now_iso()
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=timestamp,
                )
                claimed = ArchiveCatalog(connection).claim_job(
                    job_id=scheduled.job_ids[0],
                    claim_token="first-worker",
                    claimed_at=timestamp,
                    claim_expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    now=timestamp,
                    restore_due_before=timestamp,
                )
                self.assertIsNotNone(claimed)
                job = connection.execute(
                    """
                    SELECT j.*, v.source_root, v.s3_bucket, v.s3_prefix,
                           v.rclone_remote, v.encryption_mode,
                           v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                           v.uuid AS vault_uuid, v.name AS vault_name,
                           v.cloud_deletion_enabled, v.decommission_state
                    FROM jobs j JOIN vaults v ON v.id=j.vault_id
                    WHERE j.id=%s
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()

            delete_started = threading.Event()
            allow_delete = threading.Event()
            client = Mock()
            client.head_object.return_value = {"VersionId": "s3-v1", "ContentLength": 10}

            def stalled_delete(**_kwargs):
                delete_started.set()
                self.assertTrue(allow_delete.wait(timeout=10))
                return {"VersionId": "marker-after-takeover", "DeleteMarker": True}

            client.delete_object.side_effect = stalled_delete
            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            runtime = SimpleNamespace(restore_poll_interval=0)
            result: list[bool] = []
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage._runtime_settings", return_value=runtime),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                worker = threading.Thread(
                    target=lambda: result.append(storage_module.process_job(dict(job)))
                )
                worker.start()
                self.assertTrue(delete_started.wait(timeout=10))
                with SQLiteConnection(str(database_path)) as connection:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET claim_token='takeover-worker',
                            claimed_at=%s,
                            claim_expires_at=%s
                        WHERE id=%s
                        """,
                        (
                            storage_module.now_iso(),
                            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                            scheduled.job_ids[0],
                        ),
                    )
                allow_delete.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())

            self.assertEqual(result, [False])
            with SQLiteConnection(str(database_path)) as connection:
                before = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (scheduled.job_ids[0],)
                ).fetchone()
                marker_count = connection.execute(
                    "SELECT COUNT(*) AS total FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["total"]
                completed_audits = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM audit_events
                    WHERE event='cloud_deletion.archive_completed'
                    """
                ).fetchone()["total"]
                connection.execute(
                    "UPDATE jobs SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id=%s",
                    (scheduled.job_ids[0],),
                )
            self.assertEqual(before["status"], "cleaning")
            self.assertEqual(marker_count, 0)
            self.assertEqual(completed_audits, 0)

            client.list_object_versions.return_value = {
                "Versions": [],
                "DeleteMarkers": [
                    {
                        "Key": "docs/report.txt",
                        "VersionId": "marker-after-takeover",
                        "IsLatest": True,
                    }
                ],
                "IsTruncated": False,
            }
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.s3_client", return_value=client),
            ):
                summary = storage_module.reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 1, "requeued": 0, "failed": 0})
            self.assertEqual(client.delete_object.call_count, 1)
            with SQLiteConnection(str(database_path)) as connection:
                after = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (scheduled.job_ids[0],)
                ).fetchone()
                marker_count = connection.execute(
                    "SELECT COUNT(*) AS total FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["total"]
            self.assertEqual(after["status"], "completed")
            self.assertEqual(marker_count, 1)

    def test_cancellation_during_stalled_delete_does_not_publish_stale_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=1
            )
            timestamp = storage_module.now_iso()
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=timestamp,
                )
                ArchiveCatalog(connection).claim_job(
                    job_id=scheduled.job_ids[0],
                    claim_token="cancelled-worker",
                    claimed_at=timestamp,
                    claim_expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    now=timestamp,
                    restore_due_before=timestamp,
                )
                job = connection.execute(
                    """
                    SELECT j.*, v.source_root, v.s3_bucket, v.s3_prefix,
                           v.rclone_remote, v.encryption_mode,
                           v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                           v.uuid AS vault_uuid, v.name AS vault_name,
                           v.cloud_deletion_enabled, v.decommission_state
                    FROM jobs j JOIN vaults v ON v.id=j.vault_id
                    WHERE j.id=%s
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()

            entered = threading.Event()
            release = threading.Event()
            client = Mock()
            client.head_object.return_value = {"VersionId": "s3-v1"}

            def stalled_delete(**_kwargs):
                entered.set()
                self.assertTrue(release.wait(timeout=10))
                return {"VersionId": "marker-cancelled", "DeleteMarker": True}

            client.delete_object.side_effect = stalled_delete
            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            result: list[bool] = []
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage._runtime_settings", return_value=SimpleNamespace(restore_poll_interval=0)),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                worker = threading.Thread(
                    target=lambda: result.append(storage_module.process_job(dict(job)))
                )
                worker.start()
                self.assertTrue(entered.wait(timeout=10))
                with SQLiteConnection(str(database_path)) as connection:
                    cancelled = cloud_deletion.cancel_cloud_deletion(
                        connection,
                        vault_id=vault_id,
                        group_id=scheduled.group_id,
                        actor_user_id=1,
                        cancelled_at=storage_module.now_iso(),
                    )
                self.assertEqual(cancelled.cancelled_count, 1)
                release.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())

            self.assertEqual(result, [False])
            self.assertEqual(client.delete_object.call_count, 1)
            with SQLiteConnection(str(database_path)) as connection:
                state = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s", (scheduled.job_ids[0],)
                ).fetchone()["status"]
                marker_count = connection.execute(
                    "SELECT COUNT(*) AS total FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["total"]
                completed_audits = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM audit_events
                    WHERE event='cloud_deletion.archive_completed'
                    """
                ).fetchone()["total"]
            self.assertEqual(state, "cancelled")
            self.assertEqual(marker_count, 0)
            self.assertEqual(completed_audits, 0)


class ClaimLeaseRestartTests(_CloudDeletionTestCase):
    def test_restart_completes_cloud_archive_from_catalogued_delete_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=1
            )
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_archive(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now()),
                )
                catalog = ArchiveCatalog(connection)
                catalog.record_delete_marker(
                    vault_id=vault_id,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="marker-written-before-crash",
                    created_at=_iso(_now()),
                    observed_at=_iso(_now()),
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='cleaning', claim_token='dead-worker',
                        claimed_at='2026-07-23T10:00:00+00:00',
                        claim_expires_at='2000-01-01T00:00:00+00:00'
                    WHERE id=%s
                    """,
                    (scheduled.job_ids[0],),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with (
                patch("app.database.settings", database_settings),
                patch(
                    "app.storage.s3_client",
                    side_effect=AssertionError("catalog postcondition must avoid a repeat delete"),
                ),
            ):
                summary = storage_module.reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 1, "requeued": 0, "failed": 0})
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    """
                    SELECT status, claim_token, claim_expires_at
                    FROM jobs WHERE id=%s
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()
                markers = connection.execute(
                    "SELECT COUNT(*) AS total FROM delete_markers WHERE vault_file_id=%s",
                    (file_id,),
                ).fetchone()["total"]
            self.assertEqual(job["status"], "completed")
            self.assertIsNone(job["claim_token"])
            self.assertIsNone(job["claim_expires_at"])
            self.assertEqual(markers, 1)

    def test_restart_finalizes_purge_from_deleted_item_catalog_without_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, vault_id, _file_id = _prepare_vault_with_versions(
                Path(directory), cloud_deletion_enabled=True, version_count=2
            )
            with SQLiteConnection(str(database_path)) as connection:
                scheduled = cloud_deletion.schedule_cloud_purge(
                    connection,
                    vault_id=vault_id,
                    paths=["report.txt"],
                    is_directory=False,
                    actor_user_id=1,
                    requested_at=_iso(_now() - timedelta(hours=25)),
                    confirmation="Docs Archive",
                    reason="permanent cleanup",
                    generated_phrase="x",
                    delay_seconds=60,
                )
                item_ids = [
                    int(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM cloud_deletion_items WHERE job_id=%s",
                        (scheduled.job_ids[0],),
                    ).fetchall()
                ]
                cloud_deletion.mark_items_deleted(
                    connection,
                    item_ids=item_ids,
                    updated_at=_iso(_now()),
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='cleaning', claim_token='dead-worker',
                        claimed_at='2026-07-23T10:00:00+00:00',
                        claim_expires_at='2000-01-01T00:00:00+00:00'
                    WHERE id=%s
                    """,
                    (scheduled.job_ids[0],),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite", sqlite_path=str(database_path)
            )
            with patch("app.database.settings", database_settings):
                summary = storage_module.reconcile_interrupted_jobs()
            self.assertEqual(summary, {"completed": 0, "requeued": 1, "failed": 0})

            client = Mock()
            worker_settings = SimpleNamespace(operation_concurrency=1)
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()
            client.delete_objects.assert_not_called()
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    """
                    SELECT status, claim_token, claim_expires_at
                    FROM jobs WHERE id=%s
                    """,
                    (scheduled.job_ids[0],),
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertIsNone(job["claim_token"])
            self.assertIsNone(job["claim_expires_at"])


class NoncurrentLifecycleTransitionTests(_CloudDeletionTestCase):
    def test_archive_tiered_rule_transitions_noncurrent_to_cheapest_class(self) -> None:
        profile = GUIDED_PROFILES["archive_tiered"]
        rule = build_policy_lifecycle_rule(
            "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            profile,
        )
        self.assertIn("NoncurrentVersionTransitions", rule)
        cheapest = rule["NoncurrentVersionTransitions"][-1]["StorageClass"]
        self.assertEqual(cheapest, "DEEP_ARCHIVE")
        # Delete markers must never appear as content-bearing transitions.
        self.assertNotIn("DeleteMarker", str(rule))


if __name__ == "__main__":
    unittest.main()
