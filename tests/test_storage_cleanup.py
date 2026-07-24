from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from watchfiles import Change

from app import storage
from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import FileAction, cancel_job_group, free_space, jobs, queue_jobs


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(cookies={}, headers={})


from app.storage import (
    OperationCancelled,
    apply_filesystem_changes,
    cancel_jobs,
    cleanup_abandoned_restore_files,
    download_with_rclone,
    expected_cloud_key,
    object_key_to_path,
    parse_rclone_progress,
    process_free_space,
    process_job,
    process_recover,
    process_upload,
    process_jobs_once,
    reconcile_interrupted_jobs,
    rclone_remote_is_crypt,
    remove_local_copies,
    scan_tree,
    scan_cloud,
)
from tests.test_database import run_alembic


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params=()):
        self.statements.append((sql, tuple(params)))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class QueryResult:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class JobQueueResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class JobQueueConnection(RecordingConnection):
    def __init__(self, queued_jobs: list[dict[str, object]]) -> None:
        super().__init__()
        self.queued_jobs = queued_jobs
        self.jobs_by_id = {int(job["id"]): job for job in queued_jobs}

    def execute(self, sql: str, params=()):
        self.statements.append((sql, tuple(params)))
        if "SELECT status FROM jobs" in sql and params:
            job = self.jobs_by_id.get(int(params[0]))
            return JobQueueResult([{"status": job["status"]}] if job else [])
        if "j.status='queued'" in sql:
            return JobQueueResult(self.queued_jobs)
        return JobQueueResult([])


class ReconcileConnection(RecordingConnection):
    def __init__(self, jobs: list[dict[str, object]]) -> None:
        super().__init__()
        self.jobs = jobs

    def execute(self, sql: str, params=()):
        self.statements.append((sql, tuple(params)))
        if "SELECT j.*, v.source_root" in sql:
            return JobQueueResult(self.jobs)
        return QueryResult(None)


class CancelGroupConnection(RecordingConnection):
    def __init__(self, *, automatic_cleanup: bool = False) -> None:
        super().__init__()
        self.automatic_cleanup = automatic_cleanup

    def execute(self, sql: str, params=()):
        self.statements.append((sql, tuple(params)))
        if self.automatic_cleanup and "SELECT id, path, requested_by" in sql:
            return JobQueueResult(
                [
                    {
                        "id": 10,
                        "path": "docs/file.txt",
                        "requested_by": 1,
                        "archive_version_id": "version-1",
                    }
                ]
            )
        if "SELECT id FROM jobs" in sql:
            return JobQueueResult([{"id": 10}, {"id": 11}])
        return JobQueueResult([])


class StorageCleanupTests(unittest.TestCase):
    def test_cloud_keys_add_bin_only_for_crypt_remotes(self) -> None:
        self.assertEqual(
            expected_cloud_key("docs/file.txt", "archive", True),
            "archive/docs/file.txt.bin",
        )
        self.assertEqual(
            expected_cloud_key("docs/file.txt", "archive", False),
            "archive/docs/file.txt",
        )
        self.assertEqual(
            object_key_to_path("archive/docs/file.txt.bin", "archive", True),
            "docs/file.txt",
        )
        self.assertIsNone(
            object_key_to_path("archive/docs/file.txt", "archive", True)
        )
        self.assertEqual(
            object_key_to_path("archive/docs/file.txt", "archive", False),
            "docs/file.txt",
        )
        self.assertEqual(
            object_key_to_path("archive/docs/file.txt.bin", "archive", False),
            "docs/file.txt.bin",
        )

    def test_local_scan_ignores_all_rclone_recovery_temporaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "visible.txt").write_text("visible", encoding="utf-8")
            restore_id = "a" * 32
            (source / f".archive.zip.restore-{restore_id}.tmp").write_bytes(b"temp")
            (source / f".archive.zip.restore-{restore_id}.tmp.deadbeef.partial").write_bytes(
                b"partial"
            )
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                    """,
                    (str(source),),
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", test_settings):
                count = scan_tree(
                    {"id": 2, "source_root": str(source)},
                    "2026-07-20T20:00:00+00:00",
                )

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                visible = catalog.get_file_by_path(2, "visible.txt")
                temporary = catalog.get_file_by_path(
                    2, f".archive.zip.restore-{restore_id}.tmp"
                )
                partial = catalog.get_file_by_path(
                    2, f".archive.zip.restore-{restore_id}.tmp.deadbeef.partial"
                )

            self.assertEqual(count, 1)
            self.assertIsNotNone(visible)
            self.assertIsNone(temporary)
            self.assertIsNone(partial)

    def test_local_scan_updates_the_versioned_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "visible.txt").write_text("visible", encoding="utf-8")
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                    """,
                    (str(source),),
                )

            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", test_settings):
                count = scan_tree(
                    {"id": 2, "source_root": str(source)},
                    "2026-07-21T10:00:00+00:00",
                )

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "visible.txt")

            self.assertEqual(count, 1)
            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(observed["local_copy"]["size"], 7)

    def test_cloud_scan_records_the_exact_s3_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )

            page = {
                "Versions": [
                    {
                        "Key": "docs/reports/annual.txt",
                        "VersionId": "s3-version-2",
                        "Size": 21,
                        "StorageClass": "STANDARD",
                        "ETag": '"etag-2"',
                        "LastModified": datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
                    }
                ],
                "DeleteMarkers": [],
            }
            paginator = SimpleNamespace(paginate=lambda **_: [page])

            def get_paginator(name: str):
                self.assertEqual(name, "list_object_versions")
                return paginator

            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", test_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(get_paginator=get_paginator),
                ),
            ):
                count = scan_cloud(
                    {
                        "id": 2,
                        "s3_bucket": "bucket",
                        "s3_prefix": "docs",
                        "rclone_remote": "remote",
                    },
                    "2026-07-21T10:00:00+00:00",
                )

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "reports/annual.txt"
                )

            self.assertEqual(count, 1)
            self.assertEqual(
                observed["latest_version"]["provider_version_id"],
                "s3-version-2",
            )
            self.assertEqual(observed["latest_version"]["availability"], "available")
            self.assertEqual(observed["latest_version"]["integrity"], "unverified")

    def test_cloud_versions_have_deterministic_internal_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )

            page = {
                "Versions": [
                    {
                        "Key": "docs/report.txt",
                        "VersionId": "newest",
                        "Size": 20,
                        "LastModified": datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
                    },
                    {
                        "Key": "docs/report.txt",
                        "VersionId": "oldest",
                        "Size": 10,
                        "LastModified": datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
                    },
                ],
            }
            client = SimpleNamespace(
                get_paginator=lambda _: SimpleNamespace(paginate=lambda **__: [page])
            )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", test_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch("app.storage.s3_client", return_value=client),
            ):
                scan_cloud(
                    {
                        "id": 2,
                        "s3_bucket": "bucket",
                        "s3_prefix": "docs",
                        "rclone_remote": "remote",
                    },
                    "2026-07-21T10:00:00+00:00",
                )

            with SQLiteConnection(str(database_path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(
                    2, "report.txt"
                )

            self.assertEqual(
                [
                    (version["version_number"], version["provider_version_id"])
                    for version in versions
                ],
                [(2, "newest"), (1, "oldest")],
            )

    def test_upload_job_targets_a_stable_vault_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=12,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", test_settings):
                queued = queue_jobs("report.txt", "upload", 2, 1)
                listed = jobs(_fake_request(), {"id": 2})

            self.assertEqual(queued["item_count"], 1)
            self.assertEqual(listed["items"][0]["path"], "report.txt")
            self.assertEqual(listed["items"][0]["action"], "upload")

    def test_upload_without_read_back_leaves_an_unverified_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "report.txt").write_text("content", encoding="utf-8")
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO users(
                        id, username, display_name, password_hash, is_admin
                    ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                    """,
                    (str(source),),
                )
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=7,
                    mtime_ns=(source / "report.txt").stat().st_mtime_ns,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("report.txt", "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone"),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **_: {
                                "VersionId": "s3-version-1",
                                "ContentLength": 7,
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            }
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")

            self.assertEqual(
                observed["latest_version"]["provider_version_id"],
                "s3-version-1",
            )
            self.assertEqual(observed["latest_version"]["integrity"], "unverified")
            self.assertEqual(observed["latest_version"]["availability"], "available")

    def test_filesystem_events_update_only_changed_catalog_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            docs = source / "docs"
            docs.mkdir(parents=True)
            added = docs / "added.txt"
            added.write_text("new", encoding="utf-8")
            deleted = docs / "deleted.txt"
            restore_id = "b" * 32
            partial = docs / f".large.zip.restore-{restore_id}.tmp.random.partial"
            partial.write_bytes(b"partial")
            changes = {
                (Change.added, str(added.resolve())),
                (Change.deleted, str(deleted.resolve())),
                (Change.modified, str(docs.resolve())),
                (Change.added, str(partial.resolve())),
            }
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as database:
                database.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                    """,
                    (str(source),),
                )
                ArchiveCatalog(database).observe_local_copy(
                    vault_id=2,
                    path="docs/deleted.txt",
                    file_type="regular",
                    size=3,
                    mtime_ns=10,
                    observed_at="2026-07-20T19:00:00+00:00",
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", test_settings),
                patch(
                    "app.storage.now_iso",
                    return_value="2026-07-20T20:00:00+00:00",
                ),
            ):
                changed = apply_filesystem_changes(
                    {"id": 2, "source_root": str(source)}, changes
                )

            with SQLiteConnection(str(database_path)) as database:
                catalog = ArchiveCatalog(database)
                added_file = catalog.get_file_by_path(2, "docs/added.txt")
                deleted_file = catalog.get_file_by_path(2, "docs/deleted.txt")
                partial_file = catalog.get_file_by_path(
                    2, f"docs/{partial.name}"
                )

            self.assertEqual(changed, 2)
            self.assertEqual(added_file["local_copy"]["presence"], "present")
            self.assertEqual(deleted_file["local_copy"]["presence"], "missing")
            self.assertIsNone(partial_file)

    def test_remote_type_is_read_from_rclone_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "rclone.conf"
            config.write_text(
                "[encrypted]\ntype = crypt\n\n[plain]\ntype = alias\n",
                encoding="utf-8",
            )
            with patch(
                "app.storage.settings",
                SimpleNamespace(rclone_config=str(config)),
            ):
                self.assertTrue(rclone_remote_is_crypt("encrypted"))
                self.assertFalse(rclone_remote_is_crypt("plain"))

    def test_rclone_json_stats_expose_real_transferred_bytes(self) -> None:
        line = (
            '{"level":"notice","stats":{"bytes":6844416,'
            '"totalBytes":12582912},"source":"accounting/stats.go:537"}'
        )

        self.assertEqual(parse_rclone_progress(line), (6_844_416, 12_582_912))
        self.assertIsNone(parse_rclone_progress("unstructured message"))

    def test_cancelled_operations_do_not_start(self) -> None:
        operations = (
            (987654, process_upload, "Upload stopped"),
            (987655, process_recover, "Recovery stopped"),
            (987656, process_free_space, "Freeing local space stopped"),
        )
        for job_id, operation, message in operations:
            with self.subTest(operation=operation.__name__):
                cancel_jobs([job_id])
                try:
                    with self.assertRaisesRegex(OperationCancelled, message):
                        operation({"id": job_id})
                finally:
                    with storage.operation_process_lock:
                        storage.cancelled_jobs.discard(job_id)

    def test_cancelling_an_active_transfer_terminates_rclone(self) -> None:
        job_id = 987655
        process = SimpleNamespace(
            poll=lambda: None,
            terminate=Mock(),
        )
        with storage.operation_process_lock:
            storage.active_operation_processes[job_id] = process
        try:
            cancel_jobs([job_id])
            process.terminate.assert_called_once_with()
        finally:
            with storage.operation_process_lock:
                storage.active_operation_processes.pop(job_id, None)
                storage.cancelled_jobs.discard(job_id)

    def test_bug_001_recover_requires_status_whitelist(self) -> None:
        """[BUG-001][Req: REQ-002] recover dispatcher must whitelist statuses.

        A recover Job outside {queued, retrying, restoring} (e.g.
        pending_approval) must not enter process_recover; process_job returns
        False, matching peer action whitelists.
        """
        job = {
            "id": 42,
            "vault_id": 10,
            "action": "recover",
            "status": "pending_approval",
            "updated_at": "2026-07-20T19:00:00+00:00",
        }
        connection = JobQueueConnection([job])
        with (
            patch("app.storage.db", return_value=connection),
            patch("app.storage.process_recover") as mock_recover,
        ):
            processed = process_job(dict(job))

        self.assertFalse(
            processed,
            "process_job must no-op for recover outside the status whitelist",
        )
        mock_recover.assert_not_called()

    def test_queue_processes_upload_recover_and_cleanup_in_parallel(self) -> None:
        jobs = [
            {"id": 1, "vault_id": 10, "action": "upload", "status": "queued"},
            {"id": 2, "vault_id": 10, "action": "recover", "status": "queued"},
            {"id": 3, "vault_id": 10, "action": "free-space", "status": "queued"},
        ]
        connection = JobQueueConnection(jobs)
        barrier = threading.Barrier(3)
        counter_lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_operation(job):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait(timeout=1)
            finally:
                with counter_lock:
                    active -= 1

        test_settings = SimpleNamespace(operation_concurrency=3, restore_poll_interval=900)
        with (
            patch("app.storage.db", return_value=connection),
            patch("app.storage.settings", test_settings),
            patch("app.storage.get_policy", return_value=__import__(
                "app.services.operation_policies", fromlist=["OperationPolicy"]
            ).OperationPolicy()),
            patch("app.storage.process_upload", side_effect=fake_operation),
            patch("app.storage.process_recover", side_effect=fake_operation),
            patch("app.storage.process_free_space", side_effect=fake_operation),
        ):
            processed = process_jobs_once()

        self.assertEqual(processed, 3)
        self.assertEqual(max_active, 3)

    def test_restart_reconciles_completed_and_incomplete_recoveries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            complete = source / "docs" / "complete.txt"
            incomplete = source / "docs" / "incomplete.txt"
            complete.parent.mkdir(parents=True)
            complete.write_bytes(b"complete")
            abandoned = incomplete.with_name(
                f".{incomplete.name}.restore-abandoned.tmp"
            )
            abandoned.write_bytes(b"partial")
            rclone_partial = incomplete.with_name(
                f".{incomplete.name}.restore-abandoned.tmp.random.partial"
            )
            rclone_partial.write_bytes(b"partial")
            jobs = [
                {
                    "id": 10,
                    "vault_id": 2,
                    "path": "docs/complete.txt",
                    "action": "recover",
                    "status": "downloading",
                    "requested_at": "2026-07-20T19:00:00+00:00",
                    "total_bytes": 8,
                    "source_root": str(source),
                },
                {
                    "id": 11,
                    "vault_id": 2,
                    "path": "docs/incomplete.txt",
                    "action": "recover",
                    "status": "downloading",
                    "requested_at": "2026-07-20T19:00:01+00:00",
                    "total_bytes": 20,
                    "source_root": str(source),
                },
            ]
            connection = ReconcileConnection(jobs)

            with (
                patch("app.storage.db", return_value=connection),
                patch("app.storage.now_iso", return_value="2026-07-20T19:30:00+00:00"),
            ):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 0, "requeued": 2, "failed": 0})
            self.assertFalse(abandoned.exists())
            self.assertFalse(rclone_partial.exists())
            self.assertFalse(complete.exists())
            queued_ids = [
                params[-1]
                for sql, params in connection.statements
                if "UPDATE jobs SET status='queued'" in sql
            ]
            self.assertEqual(sorted(queued_ids), [10, 11])

    def test_restart_requeues_upload_and_completes_finished_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            jobs = [
                {
                    "id": 20,
                    "vault_id": 2,
                    "path": "docs/upload.txt",
                    "action": "upload",
                    "status": "uploading",
                    "requested_at": "2026-07-20T19:00:00+00:00",
                    "total_bytes": 10,
                    "source_root": str(source),
                },
                {
                    "id": 21,
                    "vault_id": 2,
                    "vault_file_id": "vault-file-21",
                    "path": "docs/deleted.txt",
                    "action": "free-space",
                    "status": "cleaning",
                    "requested_at": "2026-07-20T19:00:01+00:00",
                    "total_bytes": 15,
                    "source_root": str(source),
                },
            ]
            connection = ReconcileConnection(jobs)

            with (
                patch("app.storage.db", return_value=connection),
                patch("app.storage.now_iso", return_value="2026-07-20T19:30:00+00:00"),
            ):
                summary = reconcile_interrupted_jobs()

            self.assertEqual(summary, {"completed": 1, "requeued": 1, "failed": 0})
            job_updates = [
                (sql, params)
                for sql, params in connection.statements
                if "UPDATE jobs SET status=" in sql
            ]
            self.assertTrue(
                any("status='queued'" in sql and params[-1] == 20 for sql, params in job_updates)
            )
            self.assertTrue(
                any("status='completed'" in sql and params[-1] == 21 for sql, params in job_updates)
            )

    def test_bug_006_startup_preserves_free_space_claims(self) -> None:
        """[BUG-006][Req: REQ-009] free-space claims survive startup cleanup+reconcile.

        Seam: ``cleanup_abandoned_restore_files`` then ``reconcile_interrupted_jobs``
        (same order as lifespan). Plants a mid-claim ``.cleanup-*.tmp`` Local Copy
        with a ``cleaning`` free-space Job and asserts startup restores the claim
        and requeues the Job instead of deleting the claim and marking freed.
        """
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            payload = target.read_bytes()
            claim = target.with_name(
                f".{target.name}.cleanup-{'ab' * 16}.tmp"
            )
            target.rename(claim)
            self.assertFalse(target.exists())
            self.assertTrue(claim.is_file())

            with SQLiteConnection(str(database_path)) as connection:
                file_row = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
                job_id = connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at, total_bytes
                    ) VALUES (
                        2, %s, 'docs/file.txt', 'free-space', 'cleaning',
                        1, '2026-07-21T10:05:00+00:00',
                        '2026-07-21T10:05:00+00:00', %s
                    )
                    RETURNING id
                    """,
                    (file_row["id"], len(payload)),
                ).fetchone()["id"]

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", database_settings),
            ):
                # Lifespan order: abandoned-restore cleanup, then reconcile.
                cleanup_abandoned_restore_files()
                summary = reconcile_interrupted_jobs()

            self.assertTrue(
                target.is_file(),
                "startup must restore the free-space claim to the Local Copy path",
            )
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(
                claim.exists(),
                "restored claim should no longer remain at the temporary path",
            )
            self.assertEqual(summary["completed"], 0)
            self.assertEqual(summary["requeued"], 1)
            self.assertEqual(summary["failed"], 0)

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
            self.assertEqual(job["status"], "queued")
            self.assertNotIn("Local space freed", job["message"] or "")
            self.assertEqual(observed["local_copy"]["presence"], "present")

    def test_bug_006_failed_claim_restore_does_not_mark_freed(self) -> None:
        """[BUG-006] surviving claims must not complete free-space as freed."""
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            payload = target.read_bytes()
            claim = target.with_name(
                f".{target.name}.cleanup-{'cd' * 16}.tmp"
            )
            target.rename(claim)

            with SQLiteConnection(str(database_path)) as connection:
                file_row = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
                job_id = connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, path, action, status,
                        requested_by, requested_at, updated_at, total_bytes
                    ) VALUES (
                        2, %s, 'docs/file.txt', 'free-space', 'cleaning',
                        1, '2026-07-21T10:05:00+00:00',
                        '2026-07-21T10:05:00+00:00', %s
                    )
                    RETURNING id
                    """,
                    (file_row["id"], len(payload)),
                ).fetchone()["id"]

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", database_settings),
                patch(
                    "app.storage.restore_claimed_local_copy",
                    return_value=False,
                ),
            ):
                cleanup_abandoned_restore_files()
                summary = reconcile_interrupted_jobs()

            self.assertTrue(claim.is_file())
            self.assertEqual(claim.read_bytes(), payload)
            self.assertFalse(target.exists())
            self.assertEqual(summary["completed"], 0)
            self.assertEqual(summary["failed"], 1)
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertIn(str(claim), job["message"])
            self.assertNotIn("Local space freed", job["message"] or "")

    def test_all_operation_groups_can_be_cancelled(self) -> None:
        expected_messages = {
            "upload": "Upload stopped",
            "recover": "Recovery stopped",
            "free-space": "Freeing local space stopped",
        }
        for action, expected_message in expected_messages.items():
            with self.subTest(action=action):
                connection = CancelGroupConnection()
                with (
                    patch("app.main.db", return_value=connection),
                    patch("app.main.cancel_jobs") as cancel_jobs_mock,
                ):
                    result = cancel_job_group(
                        "group-123", action, {"id": 2, "role": "owner"}
                    )

                cancel_jobs_mock.assert_called_once_with([10, 11])
                self.assertEqual(result["message"], expected_message)
                select_params = next(
                    params
                    for sql, params in connection.statements
                    if "SELECT id FROM jobs" in sql
                )
                self.assertEqual(select_params, (2, "group-123", action))
                if action == "recover":
                    # BUG-018 / REQ-030: cancel must not wipe Glacier restore_state.
                    self.assertFalse(
                        any(
                            "restore_state=NULL" in sql
                            for sql, _ in connection.statements
                        )
                    )

    def test_cancelling_automatic_cleanup_is_audited_and_notified(self) -> None:
        connection = CancelGroupConnection(automatic_cleanup=True)
        with (
            patch("app.main.db", return_value=connection),
            patch("app.main.cancel_jobs"),
            patch(
                "app.main.audit_event_store.record_audit_event"
            ) as record_audit_event,
            patch(
                "app.main.notification_service.enqueue_notification"
            ) as enqueue_notification,
        ):
            cancel_job_group(
                "group-123",
                "free-space",
                {"id": 2, "role": "owner", "member_user_id": 7},
            )

        record_audit_event.assert_called_once_with(
            connection,
            event="local_cleanup.cancelled",
            actor_user_id=7,
            vault_id=2,
            job_id=10,
            outcome="cancelled",
            path="docs/file.txt",
            archive_version_id="version-1",
        )
        enqueue_notification.assert_called_once_with(
            connection,
            user_id=1,
            event="local_cleanup.cancelled",
            title="Automatic local cleanup cancelled",
            body="The Local Copy cleanup for docs/file.txt was cancelled.",
            vault_id=2,
            job_id=10,
        )

    def test_vault_scan_queues_due_local_cleanup_through_policy_scheduler(self) -> None:
        from app.storage import scan_vault

        vault = {
            "id": 987,
            "source_root": "/source",
            "s3_bucket": "bucket",
        }
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"user_id": 42}
        database = MagicMock()
        database.__enter__.return_value = connection
        with (
            patch("app.storage.scan_tree", return_value=1),
            patch("app.storage.apply_auto_renames", return_value={}),
            patch("app.storage.scan_cloud", return_value=1),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.db", return_value=database),
            patch("app.storage.s3_client"),
            patch("app.storage.reconcile_pending_policy_tags", return_value=0),
            patch("app.storage.sync_lifecycle_rules_for_bucket", return_value=0),
            patch("app.storage.queue_auto_uploads", return_value=2),
            patch(
                "app.storage.queue_auto_local_cleanups", return_value=3
            ) as cleanup_scheduler,
            patch(
                "app.storage.settings",
                SimpleNamespace(allow_local_delete=True),
            ),
        ):
            result = scan_vault(vault)

        self.assertEqual(result["auto_local_cleanups"], 3)
        cleanup_scheduler.assert_called_once_with(
            connection,
            vault_id=987,
            requested_by=42,
            local_delete_enabled=True,
        )

    def cleanup_fixture(self, directory: str):
        base = Path(directory)
        source = base / "source"
        target = source / "docs" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("content", encoding="utf-8")
        database_path = base / "catalog.db"
        migrated = run_alembic(database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        digest = hashlib.sha256(b"content").hexdigest()
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(
                    id, username, display_name, password_hash, is_admin
                ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote
                ) VALUES (
                    2, 'docs', 'Docs', %s, 'bucket-test', 'archive', 'remote-test'
                )
                """,
                (str(source),),
            )
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="docs/file.txt",
                file_type="regular",
                size=target.stat().st_size,
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-21T10:00:00+00:00",
            )
            version_id = catalog.record_archive_version(
                vault_id=2,
                path="docs/file.txt",
                object_key="archive/docs/file.txt.bin",
                provider_version_id="s3-version-1",
                size=target.stat().st_size,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-07-21T10:00:00+00:00",
                observed_at="2026-07-21T10:00:00+00:00",
                scan_id="2026-07-21T10:00:00+00:00",
            )
            catalog.mark_version_verified(
                version_id,
                plaintext_sha256=digest,
                verified_at="2026-07-21T10:01:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="docs/file.txt",
                plaintext_sha256=digest,
                matched_archive_version_id=version_id,
            )
        return target, database_path

    def test_free_space_queues_background_jobs(self) -> None:
        vault = {"id": 2, "role": "owner"}
        queued = {
            "group_id": "cleanup-group",
            "job_ids": [10],
            "item_count": 1,
            "total_bytes": 9,
        }
        with (
            patch("app.main.queue_jobs", return_value=queued) as queue_jobs,
            patch("app.main.settings", SimpleNamespace(allow_local_delete=True)),
        ):
            result = free_space(
                FileAction(path="docs/file.txt"),
                _fake_request(),
                {"id": 7},
                vault,
            )

        queue_jobs.assert_called_once_with(
            "docs/file.txt", "free-space", 2, 7, False
        )
        self.assertEqual(result["group_id"], "cleanup-group")
        self.assertEqual(result["message"], "Freeing local space started")

    def test_cleanup_job_verifies_s3_then_deletes_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        head_object=lambda **_: {"ContentLength": 7}
                    ),
                ),
            ):
                queue_jobs("docs/file.txt", "free-space", 2, 1)
                process_jobs_once()

            self.assertFalse(target.exists())
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
            self.assertEqual(observed["local_copy"]["presence"], "missing")

    def test_automatic_cleanup_records_completion_and_keeps_recovery_available(self) -> None:
        from app.services.operation_policies import (
            OperationPolicy,
            queue_auto_local_cleanups,
            set_policy,
        )

        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            with SQLiteConnection(str(database_path)) as connection:
                set_policy(
                    connection,
                    2,
                    OperationPolicy(
                        auto_local_cleanup=True,
                        local_retention_days=30,
                    ),
                )
                queued = queue_auto_local_cleanups(
                    connection,
                    vault_id=2,
                    requested_by=1,
                    local_delete_enabled=True,
                    now=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        head_object=lambda **_: {"ContentLength": 7}
                    ),
                ),
            ):
                processed = process_jobs_once()

            self.assertEqual(queued, 1)
            self.assertEqual(processed, 1)
            self.assertFalse(target.exists())
            with SQLiteConnection(str(database_path)) as connection:
                file_row = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
                events = connection.execute(
                    "SELECT event, outcome FROM audit_events ORDER BY id"
                ).fetchall()
                notifications = connection.execute(
                    "SELECT event FROM notifications ORDER BY id"
                ).fetchall()
            self.assertEqual(file_row["local_copy"]["presence"], "missing")
            self.assertEqual(file_row["latest_version"]["availability"], "available")
            self.assertEqual(
                events,
                [
                    {"event": "local_cleanup.auto_queued", "outcome": "queued"},
                    {"event": "local_cleanup.completed", "outcome": "success"},
                ],
            )
            self.assertEqual(
                notifications,
                [
                    {"event": "local_cleanup.auto_queued"},
                    {"event": "local_cleanup.completed"},
                ],
            )

    def test_cleanup_job_keeps_local_file_when_s3_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)

            def failed_head(**kwargs):
                raise RuntimeError("S3 is unreachable")

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(head_object=failed_head),
                ),
            ):
                queue_jobs("docs/file.txt", "free-space", 2, 1)
                process_jobs_once()

            self.assertTrue(target.exists())
            with patch("app.database.settings", database_settings):
                listed = jobs(_fake_request(), {"id": 2})
            self.assertEqual(listed["items"][0]["status"], "failed")
            self.assertIn(
                "unable to verify the S3 copy",
                listed["items"][0]["message"],
            )

    def test_automatic_cleanup_failure_is_audited_and_notified(self) -> None:
        from app.services.operation_policies import (
            OperationPolicy,
            queue_auto_local_cleanups,
            set_policy,
        )

        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            with SQLiteConnection(str(database_path)) as connection:
                set_policy(
                    connection,
                    2,
                    OperationPolicy(
                        auto_local_cleanup=True,
                        local_retention_days=30,
                    ),
                )
                queue_auto_local_cleanups(
                    connection,
                    vault_id=2,
                    requested_by=1,
                    local_delete_enabled=True,
                    now=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
                )

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        head_object=Mock(side_effect=RuntimeError("S3 unavailable"))
                    ),
                ),
            ):
                process_jobs_once()

            self.assertTrue(target.exists())
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE origin='automatic'"
                ).fetchone()
                events = connection.execute(
                    "SELECT event, outcome FROM audit_events ORDER BY id"
                ).fetchall()
                notifications = connection.execute(
                    "SELECT event FROM notifications ORDER BY id"
                ).fetchall()
            self.assertEqual(job["status"], "failed")
            self.assertIn("unable to verify the S3 copy", job["message"])
            self.assertEqual(
                events[-1],
                {"event": "local_cleanup.failed", "outcome": "failure"},
            )
            self.assertEqual(
                notifications[-1],
                {"event": "local_cleanup.failed"},
            )

    def test_cleanup_never_deletes_content_changed_after_fingerprinting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            target.write_text("changed", encoding="utf-8")
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            head_object = Mock(return_value={"ContentLength": 7})
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(head_object=head_object),
                ),
            ):
                queue_jobs("docs/file.txt", "free-space", 2, 1)
                process_jobs_once()

            self.assertEqual(target.read_text(encoding="utf-8"), "changed")
            head_object.assert_called_once_with(
                Bucket="bucket-test",
                Key="archive/docs/file.txt.bin",
                VersionId="s3-version-1",
            )
            with patch("app.database.settings", database_settings):
                listed = jobs(_fake_request(), {"id": 2})
            self.assertEqual(listed["items"][0]["status"], "failed")
            self.assertIn("changed since fingerprinting", listed["items"][0]["message"])

    def test_cleanup_preserves_a_file_replaced_during_atomic_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                allow_local_delete=True,
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            original_rename = Path.rename

            def rename_then_replace(path: Path, destination: Path):
                renamed = original_rename(path, destination)
                if path.name == target.name:
                    target.write_text("newer content", encoding="utf-8")
                return renamed

            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        head_object=lambda **_: {"ContentLength": 7}
                    ),
                ),
                patch.object(Path, "rename", rename_then_replace),
            ):
                queue_jobs("docs/file.txt", "free-space", 2, 1)
                process_jobs_once()

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "newer content",
            )
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(observed["local_copy"]["size"], len("newer content"))

    def test_recovery_pins_version_id_and_skips_path_only_rclone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, database_path = self.cleanup_fixture(directory)
            payload = target.read_bytes()
            target.unlink()
            with SQLiteConnection(str(database_path)) as connection:
                file = ArchiveCatalog(connection).get_file_by_path(2, "docs/file.txt")
                ArchiveCatalog(connection).mark_local_copy_missing(
                    file["id"], observed_at="2026-07-21T11:00:00+00:00"
                )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
                restore_days=3,
                restore_tier="Bulk",
            )

            class _Body:
                def __init__(self, data: bytes) -> None:
                    self._data = data

                def iter_chunks(self, chunk_size: int = 1024 * 1024):
                    for index in range(0, len(self._data), chunk_size):
                        yield self._data[index : index + chunk_size]

            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": len(payload),
                    "StorageClass": "STANDARD",
                    "VersionId": "s3-version-1",
                }
            )
            client.get_object = Mock(
                return_value={
                    "Body": _Body(payload),
                    "ContentLength": len(payload),
                    "VersionId": "s3-version-1",
                }
            )
            client.restore_object = Mock()
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch("app.storage.s3_client", return_value=client),
                patch("app.storage.run_rclone") as run_rclone,
            ):
                queue_jobs("docs/file.txt", "recover", 2, 1)
                process_jobs_once()

            client.head_object.assert_called()
            client.get_object.assert_called()
            self.assertEqual(
                client.get_object.call_args.kwargs["VersionId"],
                "s3-version-1",
            )
            client.restore_object.assert_not_called()
            run_rclone.assert_not_called()
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), payload)
            with patch("app.database.settings", database_settings):
                listed = jobs(_fake_request(), {"id": 2})
            self.assertEqual(listed["items"][0]["status"], "completed")

    def test_remove_local_copies_removes_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            restore = base / "restore"
            for root in (source, restore):
                target = root / "docs" / "file.txt"
                target.parent.mkdir(parents=True)
                target.write_text("content", encoding="utf-8")

            deleted = remove_local_copies({"source_root": str(source)}, "docs/file.txt")

            self.assertEqual(len(deleted), 1)
            self.assertFalse((source / "docs" / "file.txt").exists())
            self.assertTrue((restore / "docs" / "file.txt").exists())

    def test_download_restores_atomically_to_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            restore = base / "restore"
            database_path = base / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
                    """,
                    (str(source),),
                )
                ArchiveCatalog(connection).record_archive_version(
                    vault_id=2,
                    path="docs/file.txt",
                    object_key="docs/file.txt",
                    provider_version_id="version-1",
                    size=9,
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="2026-07-21T10:00:00+00:00",
                )
            job = {
                "id": 10,
                "vault_id": 2,
                "path": "docs/file.txt",
                "source_root": str(source),
                "rclone_remote": "test-crypt",
            }

            def fake_rclone(*args: str, **kwargs) -> None:
                Path(args[2]).write_text("dal cloud", encoding="utf-8")

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with (
                patch("app.storage.run_rclone", side_effect=fake_rclone),
                patch("app.storage.rclone_remote_is_crypt", return_value=True),
                patch("app.database.settings", database_settings),
                patch("app.storage.set_job"),
            ):
                download_with_rclone(job)

            target = source / "docs" / "file.txt"
            self.assertEqual(target.read_text(encoding="utf-8"), "dal cloud")
            self.assertFalse((restore / "docs" / "file.txt").exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "docs/file.txt"
                )
            self.assertEqual(observed["local_copy"]["presence"], "present")

    def test_failed_download_does_not_leave_a_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            job = {
                "id": 10,
                "vault_id": 2,
                "path": "docs/file.txt",
                "source_root": str(source),
                "rclone_remote": "test-crypt",
            }

            def failing_rclone(*args: str, **kwargs) -> None:
                Path(args[2]).write_text("parziale", encoding="utf-8")
                raise RuntimeError("download stopped")

            with (
                patch("app.storage.run_rclone", side_effect=failing_rclone),
                patch("app.storage.rclone_remote_is_crypt", return_value=True),
                patch("app.storage.set_job"),
            ):
                with self.assertRaisesRegex(RuntimeError, "download stopped"):
                    download_with_rclone(job)

            target = source / "docs" / "file.txt"
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
