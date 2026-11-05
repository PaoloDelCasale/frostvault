"""#428 PostgreSQL regression coverage; runs in PR CI, never a live database."""
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
from app.services import cloud_deletion as deletion, protection_stop, vault_relocation
from app import storage
from tests.postgres import PostgresConnection, database_dsn
from tests.test_database import run_alembic

START = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
DUE = START + timedelta(days=7)


class CloudDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "catalog.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.source = self.root / "source"
        self.source.mkdir()
        for target, value in (("app.services.source_layout.vault_local_access",
                               SimpleNamespace(local_operations_allowed=True)),
                              ("app.services.source_layout.path_is_mount", False),
                              ("app.services.cloud_deletion.now_iso", START.isoformat())):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.connection() as connection:
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
            connection.execute("""INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix,
                root_identity_version,root_identity) VALUES (1,'photos','Photos',%s,'bucket','vaults/test/',%s,%s)""",
                               (str(self.source), vault_relocation.ROOT_IDENTITY_VERSION, vault_relocation.root_identity(self.source)))
            self.file_id = self.add_file(connection, "photo.jpg", count=2)
        storage.cancelled_jobs.clear()
        self.addCleanup(storage.cancelled_jobs.clear)

    def connection(self):
        return PostgresConnection(str(self.path))

    def add_file(self, connection, path, *, count=1):
        catalog = ArchiveCatalog(connection)
        file_id = catalog.observe_local_copy(vault_id=1, path=path, file_type="regular", size=5,
                                              mtime_ns=1, observed_at=START.isoformat())
        for number in range(count):
            version = catalog.record_archive_version(vault_id=1, path=path, object_key=f"vaults/test/{path}",
                provider_version_id=f"{path}-v{number}", size=5, storage_class="STANDARD", etag="etag",
                uploaded_at=(START - timedelta(days=1, seconds=-number)).isoformat(),
                observed_at=START.isoformat(), scan_id="scan", origin="upload")
            catalog.mark_version_verified(version, plaintext_sha256="a"*64, verified_at=START.isoformat())
        catalog.mark_local_copy_missing(file_id, observed_at=START.isoformat())
        return file_id

    def schedule(self, connection, **kwargs):
        values = dict(vault_id=1, paths=["photo.jpg"], is_directory=False, actor_user_id=1,
                      requested_at=START.isoformat(), confirmation="Photos", reason="Owner deliberately deletes this file")
        values.update(kwargs)
        return deletion.schedule_cloud_purge(connection, **values)

    def claim(self, scheduled, at=DUE):
        with self.connection() as connection:
            rows = ArchiveCatalog(connection).claim_purge_group(
                lead_job_id=scheduled.job_ids[0], claim_token="worker", claimed_at=at.isoformat(),
                claim_expires_at=(at+timedelta(minutes=10)).isoformat(), now=at.isoformat(),
                message="deleting", message_key="job.cloud_purge_deleting")
            self.assertTrue(rows)
            job = connection.execute("""SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix,
                v.root_identity,v.root_identity_version,v.relocation_state,v.decommission_state
                FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s""", (scheduled.job_ids[0],)).fetchone()
        return job

    def execute(self, scheduled, at=DUE, client=None):
        job = self.claim(scheduled, at)
        client = client or Mock()
        client.delete_objects.return_value = {}
        with patch("app.database.settings", database_dsn=database_dsn(str(self.path))), \
             patch("app.storage.now_iso", return_value=at.isoformat()), \
             patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client", return_value=client):
            storage.process_cloud_purge(job)
        return client

    def test_exact_seven_day_delay_and_cancellation_prevents_every_delete(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
            self.assertEqual(datetime.fromisoformat(scheduled.pending_until), DUE)
            self.assertEqual(len(scheduled.job_ids), 1)
            items = connection.execute("SELECT provider_version_id FROM cloud_deletion_items ORDER BY id").fetchall()
            self.assertEqual([r["provider_version_id"] for r in items], ["photo.jpg-v0", "photo.jpg-v1"])
            claimed = ArchiveCatalog(connection).claim_purge_group(lead_job_id=scheduled.job_ids[0],
                claim_token="early", claimed_at=(DUE-timedelta(seconds=1)).isoformat(),
                claim_expires_at=(DUE+timedelta(minutes=10)).isoformat(), now=(DUE-timedelta(seconds=1)).isoformat(),
                message="deleting", message_key="job.cloud_purge_deleting")
            self.assertEqual(claimed, [])
            deletion.cancel_cloud_deletion(connection, vault_id=1, group_id=scheduled.group_id,
                actor_user_id=1, cancelled_at=(DUE-timedelta(seconds=1)).isoformat())
        with self.connection() as connection:
            self.assertFalse(ArchiveCatalog(connection).claim_purge_group(lead_job_id=scheduled.job_ids[0],
                claim_token="late", claimed_at=DUE.isoformat(), claim_expires_at=(DUE+timedelta(hours=1)).isoformat(),
                now=DUE.isoformat(), message="deleting", message_key="job.cloud_purge_deleting"))
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='available'").fetchone()["n"], 2)

    def test_worker_after_seven_days_deletes_only_captured_exact_version_ids(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        client = self.execute(scheduled)
        self.assertEqual(client.delete_objects.call_args.kwargs["Delete"], {"Objects":[
            {"Key":"vaults/test/photo.jpg", "VersionId":"photo.jpg-v0"},
            {"Key":"vaults/test/photo.jpg", "VersionId":"photo.jpg-v1"}], "Quiet":True})
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "completed")
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='purged'").fetchone()["n"], 2)

    def test_reappearing_local_file_without_watcher_cancels_worker(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        (self.source / "photo.jpg").write_bytes(b"new different content")
        client = self.execute(scheduled)
        client.delete_objects.assert_not_called()
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()["status"], "cancelled")
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='available'").fetchone()["n"], 2)

    def test_observed_reappearance_permanently_revokes_whole_group_single_and_batch(self):
        for batch in (False, True):
            with self.subTest(batch=batch), self.connection() as connection:
                connection.execute("UPDATE local_copies SET presence='missing',cloud_only_version_id=NULL")
                scheduled = self.schedule(connection)
                catalog = ArchiveCatalog(connection)
                if batch:
                    catalog.observe_local_copies_batch(vault_id=1, entries=[("photo.jpg", "regular", 4, 2, START.isoformat())])
                else:
                    catalog.observe_local_copy(vault_id=1,path="photo.jpg",file_type="regular",size=4,mtime_ns=2,observed_at=START.isoformat())
                catalog.mark_local_copy_missing(self.file_id, observed_at=START.isoformat())
                job = connection.execute("SELECT status FROM jobs WHERE id=%s", (scheduled.job_ids[0],)).fetchone()
                self.assertEqual(job["status"], "cancelled")

    def test_local_catalog_or_actual_presence_unknown_cloud_identity_and_busy_job_reject_admission(self):
        for field, value in (("presence", "present"), ("presence", "unsupported")):
            with self.subTest(value=value), self.connection() as connection:
                connection.execute(f"UPDATE local_copies SET {field}=%s", (value,))
                with self.assertRaises(deletion.CloudDeletionError): self.schedule(connection)
                connection.execute("UPDATE local_copies SET presence='missing'")
        with self.connection() as connection:
            (self.source / "photo.jpg").write_bytes(b"data")
            with self.assertRaises(deletion.CloudDeletionError): self.schedule(connection)
            (self.source / "photo.jpg").unlink()
            connection.execute("UPDATE archive_versions SET provider_version_id='null' WHERE version_number=1")
            with self.assertRaises(deletion.CloudDeletionError): self.schedule(connection)
            connection.execute("UPDATE archive_versions SET provider_version_id='photo.jpg-v0' WHERE version_number=1")
            with self.assertRaises(deletion.ConfirmationRequired): self.schedule(connection, confirmation="random client phrase")
            connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_at,updated_at)
                VALUES (1,%s,'photo.jpg','recover','queued',%s,%s)""", (self.file_id, START.isoformat(), START.isoformat()))
            with self.assertRaises(deletion.CloudDeletionError): self.schedule(connection)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM cloud_deletion_items").fetchone()["n"], 0)

    def test_new_cloud_version_after_consent_is_never_deleted(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
            catalog = ArchiveCatalog(connection)
            version = catalog.record_archive_version(vault_id=1,path="photo.jpg",object_key="vaults/test/photo.jpg",
                provider_version_id="external-new-copy",size=5,storage_class="STANDARD",etag="new",
                uploaded_at=DUE.isoformat(),observed_at=DUE.isoformat(),scan_id="new",origin="discovered")
            catalog.mark_version_verified(version,plaintext_sha256="b"*64,verified_at=DUE.isoformat())
        client = self.execute(scheduled)
        client.delete_objects.assert_not_called()

    def test_lost_mount_or_replaced_root_never_authorizes_cloud_deletion(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        with patch("app.services.source_layout.vault_local_access", return_value=SimpleNamespace(local_operations_allowed=False)):
            client = self.execute(scheduled)
        client.delete_objects.assert_not_called()

    def test_stop_keep_retains_local_and_cloud_and_releases_only_this_root(self):
        (self.source / "photo.jpg").write_bytes(b"local")
        with self.connection() as connection:
            result = protection_stop.stop_protection(connection,vault_id=1,actor_user_id=1,
                cloud_disposition="keep",confirmation="Photos",reason="stop")
            self.assertTrue(result["stopped"])
            self.assertTrue(result["root_released"])
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM archive_versions WHERE availability='available'").fetchone()["n"], 2)
        self.assertEqual((self.source / "photo.jpg").read_bytes(), b"local")

    def test_stop_delete_uses_same_delay_cancellation_keeps_cloud_without_resuming_protection(self):
        with self.connection() as connection:
            result = protection_stop.stop_protection(connection,vault_id=1,actor_user_id=1,
                cloud_disposition="delete",confirmation="Photos",reason="stop")
            self.assertTrue(result["cancellable"])
            self.assertFalse(result["root_released"])
            self.assertEqual(datetime.fromisoformat(result["pending_until"]), DUE)
            with self.assertRaises(LookupError):
                ArchiveCatalog(connection).queue_jobs(vault_id=1,path="photo.jpg",action="upload",requested_by=1,
                    requested_at=START.isoformat(),group_id="watcher",is_directory=False)
            result = protection_stop.cancel_deletion(connection,vault_id=1,actor_user_id=1)
            self.assertTrue(result["stopped"])
            self.assertTrue(result["root_released"])
            self.assertEqual(result["cloud_disposition"], "keep")

    def test_stop_delete_rejects_present_file_and_never_queues_local_removal(self):
        (self.source / "photo.jpg").write_bytes(b"data")
        with self.connection() as connection:
            with self.assertRaises(deletion.CloudDeletionError):
                protection_stop.stop_protection(connection,vault_id=1,actor_user_id=1,
                    cloud_disposition="delete",confirmation="Photos",reason="stop")
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"], 0)

    def test_two_concurrent_requests_cannot_admit_duplicate_or_recovery_work(self):
        barrier = threading.Barrier(2)
        def schedule(_):
            with self.connection() as connection:
                barrier.wait(timeout=20)
                try:
                    return self.schedule(connection).group_id
                except deletion.CloudDeletionError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(schedule, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        with self.connection() as connection:
            queued, _, _ = ArchiveCatalog(connection).queue_jobs(vault_id=1,path="photo.jpg",action="recover",requested_by=1,
                requested_at=START.isoformat(),group_id="recovery",is_directory=False)
            self.assertEqual(queued, [])

    def test_cancellation_after_claim_never_claims_to_undo_provider_deletion(self):
        with self.connection() as connection:
            scheduled = self.schedule(connection)
        self.claim(scheduled)
        with self.connection() as connection:
            with self.assertRaises(deletion.CloudDeletionError):
                deletion.cancel_cloud_deletion(connection,vault_id=1,group_id=scheduled.group_id,
                    actor_user_id=1,cancelled_at=DUE.isoformat())
