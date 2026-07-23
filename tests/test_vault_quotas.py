from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.vault_quotas import (
    QuotaBlocked,
    QuotaLimits,
    set_limits,
    usage_snapshot,
)
from tests.test_database import run_alembic


class VaultQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quotas.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')"
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (2, 1, 'owner')"
            )

    def test_available_versions_and_nonterminal_jobs_are_accounted(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.record_archive_version(
                vault_id=2, path="one.txt", object_key="docs/one.txt",
                provider_version_id="v1", size=7, storage_class="STANDARD", etag="e1",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
            )
            catalog.record_archive_version(
                vault_id=2, path="two.txt", object_key="docs/two.txt",
                provider_version_id="v2", size=3, storage_class="STANDARD", etag="e2",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
            )
            connection.execute("UPDATE archive_versions SET availability='missing' WHERE provider_version_id='v2'")
            connection.execute(
                "INSERT INTO jobs(vault_id, vault_file_id, path, action, status, requested_by, requested_at, updated_at) "
                "VALUES (2, (SELECT id FROM vault_files WHERE vault_id=2 LIMIT 1), 'one.txt', 'upload', 'queued', 1, '2026-01-01', '2026-01-01')"
            )
            usage = usage_snapshot(connection, 2)
        self.assertEqual(usage["storage_bytes"], 7)
        self.assertEqual(usage["concurrency"], 1)

    def test_sequential_pending_uploads_reserve_storage(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_hard_limit_bytes=10))
            catalog = ArchiveCatalog(connection)
            for name, size in (("first.txt", 6), ("second.txt", 5)):
                catalog.observe_local_copy(
                    vault_id=2, path=name, file_type="regular", size=size,
                    mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
                )
            jobs, _, _ = catalog.queue_jobs(
                vault_id=2, path="first.txt", action="upload", requested_by=1,
                requested_at="2026-01-01T00:00:00+00:00", group_id="first", is_directory=False,
            )
            self.assertEqual(len(jobs), 1)
            with self.assertRaises(QuotaBlocked) as blocked:
                catalog.queue_jobs(
                    vault_id=2, path="second.txt", action="upload", requested_by=1,
                    requested_at="2026-01-01T00:00:00+00:00", group_id="second", is_directory=False,
                )
            self.assertEqual(blocked.exception.evaluation.decisions[0].code, "quota.storage.hard_exceeded")
            self.assertEqual(connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()["total"], 1)

    def test_concurrent_pending_uploads_reserve_storage(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_hard_limit_bytes=10))
            catalog = ArchiveCatalog(connection)
            for name in ("first.txt", "second.txt"):
                catalog.observe_local_copy(
                    vault_id=2, path=name, file_type="regular", size=6,
                    mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
                )

        def queue(name: str):
            try:
                with SQLiteConnection(str(self.path)) as connection:
                    return ArchiveCatalog(connection).queue_jobs(
                        vault_id=2, path=name, action="upload", requested_by=1,
                        requested_at="2026-01-01T00:00:00+00:00", group_id=name, is_directory=False,
                    )
            except QuotaBlocked:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(queue, ("first.txt", "second.txt")))
        self.assertEqual(sum(result is not None for result in results), 1)
        with SQLiteConnection(str(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()["total"], 1)

    def test_directory_admission_ignores_already_queued_duplicate(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(
                connection, 2,
                QuotaLimits(storage_hard_limit_bytes=6, concurrency_hard_limit=2),
            )
            catalog = ArchiveCatalog(connection)
            for name, size in (("folder/first.txt", 4), ("folder/second.txt", 2)):
                catalog.observe_local_copy(
                    vault_id=2, path=name, file_type="regular", size=size,
                    mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
                )
            first_jobs, _, _ = catalog.queue_jobs(
                vault_id=2, path="folder/first.txt", action="upload", requested_by=1,
                requested_at="2026-01-01T00:00:00+00:00", group_id="first", is_directory=False,
            )
            self.assertEqual(len(first_jobs), 1)
            jobs, total_bytes, eligible_count = catalog.queue_jobs(
                vault_id=2, path="folder", action="upload", requested_by=1,
                requested_at="2026-01-01T00:00:00+00:00", group_id="folder", is_directory=True,
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(total_bytes, 2)
            self.assertEqual(eligible_count, 1)
            self.assertEqual(
                connection.execute("SELECT path FROM jobs WHERE id=%s", (jobs[0],)).fetchone()["path"],
                "folder/second.txt",
            )
            self.assertEqual(
                catalog.last_quota_evaluation.decisions,
                (),
            )

    def test_soft_warning_admits_and_hard_block_inserts_no_jobs(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_soft_limit_bytes=4, storage_hard_limit_bytes=10))
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2, path="upload.txt", file_type="regular", size=5,
                mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
            )
            jobs, _, _ = catalog.queue_jobs(
                vault_id=2, path="upload.txt", action="upload", requested_by=1,
                requested_at="2026-01-01T00:00:00+00:00", group_id="soft", is_directory=False,
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(catalog.last_quota_evaluation.decisions[0].code, "quota.storage.soft_exceeded")

            catalog.observe_local_copy(
                vault_id=2, path="blocked.txt", file_type="regular", size=11,
                mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
            )
            with self.assertRaises(QuotaBlocked) as blocked:
                catalog.queue_jobs(
                    vault_id=2, path="blocked.txt", action="upload", requested_by=1,
                    requested_at="2026-01-01T00:00:00+00:00", group_id="hard", is_directory=False,
                )
            self.assertEqual(blocked.exception.evaluation.decisions[0].code, "quota.storage.hard_exceeded")
            self.assertEqual(connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()["total"], 1)

    def test_unknown_sizes_return_stable_unknown_codes(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(storage_hard_limit_bytes=100, restore_30d_hard_limit_bytes=100))
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2, path="unknown.txt", file_type="regular", size=None,
                mtime_ns=None, observed_at="2026-01-01T00:00:00+00:00",
            )
            with self.assertRaises(QuotaBlocked) as storage_block:
                catalog.queue_jobs(
                    vault_id=2, path="unknown.txt", action="upload", requested_by=1,
                    requested_at="2026-01-01T00:00:00+00:00", group_id="unknown-storage", is_directory=False,
                )
            self.assertEqual(storage_block.exception.evaluation.decisions[0].code, "storage.usage_unknown")

    def test_concurrent_admissions_are_serialized(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            set_limits(connection, 2, QuotaLimits(concurrency_hard_limit=1))
            catalog = ArchiveCatalog(connection)
            for name in ("a.txt", "b.txt"):
                catalog.observe_local_copy(
                    vault_id=2, path=name, file_type="regular", size=1,
                    mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00",
                )

        def queue(name: str):
            try:
                with SQLiteConnection(str(self.path)) as connection:
                    return ArchiveCatalog(connection).queue_jobs(
                        vault_id=2, path=name, action="upload", requested_by=1,
                        requested_at="2026-01-01T00:00:00+00:00", group_id=name, is_directory=False,
                    )
            except QuotaBlocked:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(queue, ("a.txt", "b.txt")))
        self.assertEqual(sum(result is not None for result in results), 1)
        with SQLiteConnection(str(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()["total"], 1)


if __name__ == "__main__":
    unittest.main()
