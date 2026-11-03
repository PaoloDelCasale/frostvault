"""#423 acceptance: real catalog/Jobs, simulated clock, no scan or S3 network."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from watchfiles import Change

from app import storage
from app.catalog import ArchiveCatalog
from app.services.operation_policies import OperationPolicy, set_policy
from app.services import vault_relocation
from tests.postgres import PostgresConnection, database_dsn, run_alembic
from tests.snapshot_s3_stub import VersionedS3Stub


class WatcherAutomaticUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.database = self.base / "catalog"
        migrated = run_alembic(self.database)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.start = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        self.clock = self.start
        self.access = SimpleNamespace(local_operations_allowed=True, volume_alias=None, volume_health="healthy")
        with self.db() as connection:
            connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1, 'owner', 'Owner', 'hash')")
            connection.execute("""
                INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix,
                                   root_identity, root_identity_version)
                VALUES (2, 'photos', 'Photos', %s, 'test-bucket', 'photos', %s, %s)
            """, (str(self.root), vault_relocation.root_identity(str(self.root)), vault_relocation.ROOT_IDENTITY_VERSION))
            self.vault = dict(connection.execute("SELECT * FROM vaults WHERE id=2").fetchone())
        outer = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.clock.astimezone(tz) if tz else outer.clock.replace(tzinfo=None)

        for patcher in (
            # Scan generations are process-local. Do not leak a simulated
            # future generation into unrelated direct cloud-scan fixtures.
            patch("app.storage.runtime_status", {}),
            patch("app.services.operation_policies.datetime", Clock),
            patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(self.database))),
            patch("app.storage.settings", SimpleNamespace(operation_concurrency=1, restore_poll_interval=900, allow_local_delete=False)),
            patch("app.storage.source_layout.vault_local_access", side_effect=lambda _: self.access),
            patch("app.storage.now_iso", side_effect=lambda: self.clock.isoformat()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def db(self):
        return PostgresConnection(self.database)

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def write(self, name="photo.jpg", content=b"photo", *, watch=True):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        # SMB/copiers can preserve old modification times. Stability starts
        # from observation, not from this old filesystem timestamp.
        os.utime(target, (1_500_000_000, 1_500_000_000))
        if watch:
            storage.apply_filesystem_changes(self.vault, {(Change.modified, str(target))})
        return target

    def queue(self):
        return storage.queue_vault_auto_uploads(self.vault, now=self.clock)

    def jobs(self):
        with self.db() as connection:
            return connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def provider(self, payload=b"photo"):
        return VersionedS3Stub(
            version_bytes=payload,
            head_object=lambda **kwargs: {
                "VersionId": kwargs.get("VersionId") or "uploaded-1",
                "ContentLength": len(payload), "StorageClass": "STANDARD", "ETag": '"etag"',
            },
        )

    def test_copy_reaches_verified_in_five_minutes_without_full_scan(self):
        with patch("app.storage.scan_vault", side_effect=AssertionError("must not scan")), patch("app.storage.s3_client") as no_cloud:
            self.write()
            self.advance(299)
            self.assertEqual(self.queue(), 0)
            self.advance(1)
            self.assertEqual(self.queue(), 1)
            self.assertEqual(self.queue(), 0)  # durable deduplication
            no_cloud.assert_not_called()
        provider = self.provider()
        with patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client", return_value=provider), patch("app.storage.upload_s3_file", return_value={"VersionId": "uploaded-1"}):
            self.assertEqual(storage.process_jobs_once(), 1)
        with self.db() as connection:
            current = ArchiveCatalog(connection).get_file_by_path(2, "photo.jpg")
        self.assertEqual(self.jobs()[0]["status"], "completed")
        self.assertEqual(self.jobs()[0]["origin"], "automatic")
        self.assertEqual(current["latest_version"]["integrity"], "verified")
        self.assertEqual(current["latest_version"]["availability"], "available")
        self.assertEqual(provider.reads[0]["VersionId"], "uploaded-1")
        self.assertEqual(provider.deleted, [])
        self.assertLess((self.clock - self.start).total_seconds(), 3600)

    def test_edit_resets_stability_even_when_size_and_mtime_are_preserved(self):
        self.write(content=b"first")
        self.advance(299)
        self.write(content=b"other")
        self.advance(1)
        self.assertEqual(self.queue(), 0)
        self.advance(298)
        self.assertEqual(self.queue(), 0)
        self.advance(1)
        self.assertEqual(self.queue(), 1)

    def test_same_size_edit_invalidates_previously_verified_fingerprint(self):
        self.write(content=b"first")
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            version = catalog.record_archive_version(
                vault_id=2, path="photo.jpg", object_key="photos/photo.jpg",
                provider_version_id="previous", size=5, storage_class="STANDARD", etag="etag",
                uploaded_at=self.clock.isoformat(), observed_at=self.clock.isoformat(), scan_id="first",
            )
            digest = hashlib.sha256(b"first").hexdigest()
            catalog.mark_version_verified(version, plaintext_sha256=digest, verified_at=self.clock.isoformat())
            catalog.set_local_fingerprint(vault_id=2, path="photo.jpg", plaintext_sha256=digest, matched_archive_version_id=version)
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        self.write(content=b"other")
        self.assertEqual(self.queue(), 0)
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT integrity, availability FROM archive_versions").fetchone(),
                             {"integrity": "verified", "availability": "available"})

    def test_disabled_or_relocation_suspended_vault_preserves_catalog(self):
        self.write()
        self.advance(300)
        with self.db() as connection:
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=2")
        self.assertEqual(self.queue(), 0)
        with self.db() as connection:
            connection.execute("UPDATE vaults SET enabled=TRUE, relocation_state='scan_required' WHERE id=2")
        self.assertEqual(self.queue(), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT presence FROM local_copies").fetchone()["presence"], "present")
        self.assertEqual(self.jobs(), [])

    def test_paused_vault_and_queued_automatic_job_wait_for_resume(self):
        self.write()
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(auto_upload=False))
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(auto_upload=True))
        self.assertEqual(self.queue(), 1)
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(auto_upload=False))
        self.assertEqual(storage.claimable_queue_depth(), 0)
        with patch("app.storage.validate_cloud_vault") as preflight, patch("app.storage.upload_s3_file") as upload:
            self.assertEqual(storage.process_jobs_once(), 0)
            preflight.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.jobs()[0]["status"], "queued")
        self.assertIsNone(self.jobs()[0]["claim_token"])

    def test_paused_automatic_jobs_do_not_starve_explicit_manual_work(self):
        self.write()
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        automatic_id = self.jobs()[0]["id"]
        self.write("manual.jpg")
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(auto_upload=False))
            catalog = ArchiveCatalog(connection)
            manual, _, _ = catalog.queue_jobs(
                vault_id=2, path="manual.jpg", action="upload", requested_by=1,
                requested_at=self.clock.isoformat(), group_id="manual", is_directory=False,
            )
            candidates = catalog.list_claimable_jobs(
                now=self.clock.isoformat(), restore_due_before=self.clock.isoformat(), limit=1,
            )
            self.assertEqual([row["id"] for row in candidates], manual)
            self.assertIsNone(catalog.claim_job(
                job_id=automatic_id, claim_token="paused", claimed_at=self.clock.isoformat(),
                claim_expires_at=(self.clock + timedelta(minutes=5)).isoformat(),
                now=self.clock.isoformat(), restore_due_before=self.clock.isoformat(),
            ))
        self.assertEqual(storage.claimable_queue_depth(), 1)

    def test_edit_after_queue_is_rechecked_before_preflight(self):
        self.write()
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        self.advance(1)
        self.write(content=b"newer")
        with patch("app.storage.validate_cloud_vault") as preflight, patch("app.storage.upload_s3_file") as upload:
            storage.process_jobs_once()
            preflight.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.jobs()[0]["status"], "queued")

    def test_edit_during_preflight_and_hashing_cannot_write_cloud_content(self):
        self.write()
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        real_hash = storage.hash_stable_regular_file

        def edit_after_hash(source):
            snapshot = real_hash(source)
            self.advance(1)
            self.write(content=b"newer")
            return snapshot

        with (
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.hash_stable_regular_file", side_effect=edit_after_hash),
            patch("app.storage.upload_s3_file") as upload,
            patch("app.storage.s3_client") as cloud,
        ):
            storage.process_jobs_once()
            upload.assert_not_called()
            cloud.assert_not_called()
        self.assertEqual(self.jobs()[0]["status"], "queued")

    def test_fixed_noise_is_never_automatically_queued(self):
        names = [".DS_Store", "nested/Thumbs.db", "._photo.jpg", "catalog.lock", "db-wal", "db-journal"]
        for name in names:
            self.write(name)
        self.write("catalog.lrcat")
        with self.db() as connection:
            set_policy(connection, 2, OperationPolicy(include_globs=("*",)))
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        self.assertEqual([job["path"] for job in self.jobs()], ["catalog.lrcat"])

    def test_observations_survive_restart_and_unchanged_reconciliation(self):
        self.write()
        self.advance(150)
        # Simulate a fresh scheduler with a reloaded Vault and a full scan.
        # Re-observing identical content must not postpone it another 5 min.
        storage.scan_tree(self.vault, self.clock.isoformat())
        with self.db() as connection:
            self.vault = dict(connection.execute("SELECT * FROM vaults WHERE id=2").fetchone())
        self.advance(150)
        self.assertEqual(self.queue(), 1)

    def test_full_scan_recovers_missed_watcher_event(self):
        self.write(watch=False)
        storage.scan_tree(self.vault, self.clock.isoformat())
        self.assertEqual(self.queue(), 0)
        self.advance(300)
        self.assertEqual(self.queue(), 1)

    def test_offline_volume_never_touches_root_or_marks_catalog_missing(self):
        self.write()
        self.advance(300)
        self.access = SimpleNamespace(local_operations_allowed=False, volume_alias="offline", volume_health="absent")
        with patch("app.storage._pinned_verified_scan_root", side_effect=AssertionError("must not open root")):
            self.assertEqual(self.queue(), 0)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT presence FROM local_copies").fetchone()["presence"], "present")
        self.assertEqual(self.jobs(), [])

    def test_replaced_root_blocks_watcher_and_queue_preserving_catalog(self):
        self.write()
        self.advance(300)
        self.root.rename(self.base / "original")
        self.root.mkdir()
        replacement = self.write(content=b"replacement", watch=False)
        with self.assertRaises(RuntimeError):
            storage.apply_filesystem_changes(self.vault, {(Change.modified, str(replacement))})
        with self.assertRaises(RuntimeError):
            self.queue()
        with self.db() as connection:
            row = connection.execute("SELECT size, presence FROM local_copies").fetchone()
        self.assertEqual(row, {"size": 5, "presence": "present"})
        self.assertEqual(self.jobs(), [])

    def test_root_replaced_during_watcher_batch_rolls_back_observations(self):
        target = self.write(watch=False)
        real_inspect = storage.inspect_entry

        def replace_root(entry):
            observed = real_inspect(entry)
            self.root.rename(self.base / "original")
            self.root.mkdir()
            return observed

        with patch("app.storage.inspect_entry", side_effect=replace_root):
            with self.assertRaises(RuntimeError):
                storage.apply_filesystem_changes(self.vault, {(Change.added, str(target))})
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM local_copies").fetchone()["n"], 0)
        self.assertEqual(self.jobs(), [])

    def test_lifecycle_and_validation_failures_do_not_block_scan_enqueue(self):
        for failure in ("lifecycle", "validation"):
            with self.subTest(failure=failure):
                name = failure + ".jpg"
                self.write(name)
                self.advance(300)
                with (
                    patch("app.storage.scan_cloud", return_value=0),
                    patch("app.storage.validate_cloud_vault", side_effect=RuntimeError("preflight unavailable") if failure == "validation" else None),
                    patch("app.storage.s3_client"),
                    patch("app.storage.sync_lifecycle_rules_for_bucket", side_effect=RuntimeError("lifecycle unavailable")),
                ):
                    result = storage.scan_vault(self.vault)
                self.assertEqual(result["auto_uploads"], 1)
                self.assertEqual(result["lifecycle_rules"], -1)
                self.assertIn("Storage plan synchronization", storage.runtime_status[2]["last_error"])
        self.assertEqual(len(self.jobs()), 2)

    def test_preflight_failure_never_writes_or_deletes_cloud_content(self):
        self.write()
        self.advance(300)
        self.assertEqual(self.queue(), 1)
        with patch("app.storage.validate_cloud_vault", side_effect=RuntimeError("AccessDenied")), patch("app.storage.s3_client") as client, patch("app.storage.upload_s3_file") as upload:
            storage.process_jobs_once()
            client.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.jobs()[0]["status"], "failed")
        # Do not silently reset the retry budget every scheduler tick.
        self.advance(300)
        self.assertEqual(self.queue(), 0)
        self.write(content=b"new content")
        self.advance(300)
        self.assertEqual(self.queue(), 1)

    def test_cloud_only_conflict_and_local_deletion_preserve_sole_cloud_copy(self):
        target = self.write()
        digest = hashlib.sha256(b"photo").hexdigest()
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            version = catalog.record_archive_version(
                vault_id=2, path="photo.jpg", object_key="photos/photo.jpg",
                provider_version_id="sole-copy", size=5, storage_class="STANDARD", etag="etag",
                uploaded_at=self.clock.isoformat(), observed_at=self.clock.isoformat(), scan_id="first",
            )
            catalog.mark_version_verified(version, plaintext_sha256=digest, verified_at=self.clock.isoformat())
            catalog.set_local_fingerprint(vault_id=2, path="photo.jpg", plaintext_sha256=digest, matched_archive_version_id=version)
        target.unlink()  # disposable fixture only
        storage.apply_filesystem_changes(self.vault, {(Change.deleted, str(target))})
        self.write(content=b"different")
        self.advance(300)
        with patch("app.storage.s3_client") as cloud:
            self.assertEqual(self.queue(), 0)
            cloud.assert_not_called()
        with self.db() as connection:
            row = connection.execute("SELECT integrity, availability FROM archive_versions WHERE id=%s", (version,)).fetchone()
            hold = connection.execute("SELECT cloud_only_version_id FROM local_copies").fetchone()
        self.assertEqual(row, {"integrity": "verified", "availability": "available"})
        self.assertEqual(hold["cloud_only_version_id"], version)
        self.assertEqual(self.jobs(), [])


class AutomaticUploadLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_watcher_still_gets_timer_ticks_and_failure_does_not_stop_loop(self):
        import asyncio

        with (
            patch("app.storage.queue_all_auto_uploads_once", side_effect=[RuntimeError("DB temporarily unavailable"), 1]) as queue,
            patch("app.storage._report_worker_error") as report,
            patch("app.storage.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await storage.auto_upload_loop()
        self.assertEqual(queue.call_count, 2)
        report.assert_called_once()
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(storage.AUTO_UPLOAD_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
