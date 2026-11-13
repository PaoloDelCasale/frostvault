"""#428 Delayed Cloud Deletion edge cases on the rebuilt model (#594, ADR-0035).

Same real admission, group claim and worker as ``tests.test_cloud_purge``
(disposable PostgreSQL 16, synthetic ``VersionedProvider``); this module keeps
the #428 regressions that suite does not already own.

Former cases now owned by ``tests.test_cloud_purge``:
- exact seven-day delay, cancellation before start →
  ``test_consent_pins_the_exact_current_and_waits_seven_days``,
  ``test_cancel_before_start_keeps_everything``;
- the worker deletes only the captured exact VersionId →
  ``test_elapsed_consent_deletes_exactly_the_current_and_its_row``;
- a reappearing file not yet seen by the watcher cancels the worker →
  ``test_a_present_entry_at_execution_cancels_without_deleting``;
- a new cloud version after consent is never deleted (the consent pins the
  Current; any change cancels) → ``test_a_changed_cloud_fact_at_execution_cancels_without_deleting``,
  ``test_new_bytes_never_inherit_the_old_consent``;
- Stop and keep / stop and delete after the delay / cancelling a stop
  deletion → ``test_stop_and_keep_releases_the_root_and_keeps_the_cloud``,
  ``test_stop_and_delete_holds_the_root_until_the_deletion_ends``,
  ``test_cancelling_a_stop_deletion_keeps_the_cloud_and_releases_the_root``.

Retired with the old model: legacy delete markers are no longer catalogued
(one exact Current per file, ADR-0035), and a Vault File always has its
catalog row, so "no local observation" is the ``local_presence IS NULL`` case
covered below.
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from app import storage
from app.catalog import ArchiveCatalog
from app.services import cloud_deletion, protection_stop
from app.services.recovery_kit import record_metadata_restore

from tests import test_cloud_purge as purge
from tests.test_upload_worker import A


class CloudDeletionFixture(unittest.TestCase):
    """The ``test_cloud_purge`` helpers, without its tests."""

    setUp = purge.CloudPurgeTests.setUp
    db = purge.CloudPurgeTests.db
    observe = purge.CloudPurgeTests.observe
    write = purge.CloudPurgeTests.write
    queue = purge.CloudPurgeTests.queue
    claim = purge.CloudPurgeTests.claim
    run_job = purge.CloudPurgeTests.run_job
    job = purge.CloudPurgeTests.job
    file_row = purge.CloudPurgeTests.file_row
    rows = purge.CloudPurgeTests.rows
    version = purge.CloudPurgeTests.version
    assert_current = purge.CloudPurgeTests.assert_current
    cloud_only = purge.CloudPurgeTests.cloud_only
    second_cloud_only = purge.CloudPurgeTests.second_cloud_only
    schedule = purge.CloudPurgeTests.schedule
    claim_group = purge.CloudPurgeTests.claim_group
    run_purge = purge.CloudPurgeTests.run_purge
    expire_group = purge.CloudPurgeTests.expire_group
    statuses = purge.CloudPurgeTests.statuses
    assert_deleted = purge.CloudPurgeTests.assert_deleted
    assert_untouched = purge.CloudPurgeTests.assert_untouched
    stop = purge.CloudPurgeTests.stop
    lifecycle = purge.CloudPurgeTests.lifecycle

    def count(self, sql: str, *params) -> int:
        return self.rows(f"SELECT count(*) AS n FROM ({sql}) q", *params)[0]["n"]


class CloudDeletionTests(CloudDeletionFixture):

    # -- admission -----------------------------------------------------------

    def test_presence_unknown_identity_and_busy_job_reject_admission(self) -> None:
        current = self.cloud_only()
        for presence in ("present", "unsupported"):
            with self.subTest(presence=presence):
                with self.db() as connection:
                    connection.execute(
                        "UPDATE files SET local_presence=%s, local_file_type=%s, local_sha256=NULL, "
                        "matched_version_id=NULL WHERE id=%s",
                        (presence, "regular" if presence == "present" else "symlink", self.file_id))
                with self.assertRaises(cloud_deletion.CloudDeletionError):
                    self.schedule()
                with self.db() as connection:
                    connection.execute(
                        "UPDATE files SET local_presence='missing', local_file_type='regular' WHERE id=%s",
                        (self.file_id,))
        with self.subTest("present on disk, not yet observed"):
            self.file.write_bytes(b"data")
            with self.assertRaisesRegex(cloud_deletion.CloudDeletionError, "present"):
                self.schedule()
            self.file.unlink()
        with self.subTest("unknown cloud identity"):
            # A Current always carries the exact VersionId its landed attempt
            # proved; the schema refuses to blur it into an unknown one.
            with self.assertRaises(psycopg.errors.RestrictViolation), self.db() as connection:
                connection.execute("UPDATE archive_versions SET provider_version_id='null' WHERE id=%s",
                                   (current["id"],))
        with self.subTest("busy file"):
            with self.db() as connection:
                connection.execute(
                    "INSERT INTO jobs(vault_id, file_id, archive_version_id, action, status, origin) "
                    "VALUES (2, %s, %s, 'recover', 'queued', 'manual')", (self.file_id, current["id"]))
            with self.assertRaisesRegex(cloud_deletion.CloudDeletionError, "pending work"):
                self.schedule()
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action='cloud-purge'"), 0)
        self.assertEqual(self.count("SELECT 1 FROM cloud_purge_requests"), 0)
        self.assert_untouched(current)

    def test_no_local_observation_still_requires_verified_cloud_and_physical_absence(self) -> None:
        current = self.cloud_only()
        with self.db() as connection:
            connection.execute(
                """UPDATE files SET local_presence=NULL, local_file_type=NULL, local_observed_at=NULL,
                       local_size=NULL, local_mtime_ns=NULL, local_sha256=NULL, local_seen_at=NULL,
                       local_content_observed_at=NULL, local_recovered_at=NULL,
                       matched_version_id=NULL WHERE id=%s""",
                (self.file_id,))
        self.file.write_bytes(b"data")
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.schedule()
        self.file.unlink()
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET integrity='unverified' WHERE id=%s",
                               (current["id"],))
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.schedule()
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET integrity='verified' WHERE id=%s",
                               (current["id"],))
        self.assertEqual(len(self.schedule().job_ids), 1)

    def test_two_concurrent_requests_cannot_admit_duplicate_or_recovery_work(self) -> None:
        self.cloud_only()
        barrier = threading.Barrier(2)

        def request(_):
            with self.db() as connection:
                barrier.wait(timeout=20)
                try:
                    return cloud_deletion.schedule_cloud_purge(
                        connection, vault_id=2, paths=["report.txt"], is_directory=False,
                        actor="owner", requested_at=purge.PAST, confirmation="Docs", reason="twice").group_id
                except cloud_deletion.CloudDeletionError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        with self.db() as connection:
            queued, _, _ = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action="recover", requested_at=purge.PAST,
                group_id="00000000-0000-0000-0000-0000000000c1", is_directory=False)
        self.assertEqual(queued, [])

    def test_directory_selection_escapes_wildcards_and_never_partially_admits_mixed_files(self) -> None:
        self.second_cloud_only("album_1/photo.jpg")
        self.second_cloud_only("albumX1/sibling.jpg")
        selected = self.schedule(paths=["album_1"], is_directory=True)
        paths = self.rows("""SELECT f.path FROM jobs j JOIN files f ON f.id=j.file_id
                             WHERE j.group_id=%s""", selected.group_id)
        self.assertEqual([r["path"] for r in paths], ["album_1/photo.jpg"])
        with self.db() as connection:
            cloud_deletion.cancel_cloud_deletion(connection, vault_id=2, group_id=selected.group_id,
                                                 actor="owner", cancelled_at=storage.now_iso())
        present = self.source / "album_1/present.jpg"
        present.write_bytes(b"local")
        with self.db() as connection:
            stat = present.stat()
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2, path="album_1/present.jpg", file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.schedule(paths=["album_1"], is_directory=True)
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE status='pending_delay'"), 0)

    def test_stop_delete_rejects_present_file_and_never_queues_local_removal(self) -> None:
        self.cloud_only()
        self.file.write_bytes(b"data")
        with self.assertRaises(cloud_deletion.CloudDeletionError):
            self.stop("delete")
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action<>'upload'"), 0)
        self.assertEqual(self.lifecycle(), "active")
        self.assertEqual(self.file.read_bytes(), b"data")

    def test_stop_delete_blocks_new_uploads_until_cancelled(self) -> None:
        self.cloud_only()
        status = self.stop("delete")
        self.assertEqual(self.lifecycle(), "stopping")
        with self.db() as connection, self.assertRaises(LookupError):
            ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action="upload", requested_at=storage.now_iso(),
                group_id="00000000-0000-0000-0000-0000000000c2", is_directory=False)
        with self.db() as connection:
            status = protection_stop.cancel_deletion(connection, vault_id=2, actor="owner")
        self.assertEqual((status["stopped"], status["root_released"], status["cloud_disposition"]),
                         (True, True, "keep"))

    def test_keep_cloud_can_stop_an_unavailable_reconnect_without_reading_or_deleting_data(self) -> None:
        current = self.cloud_only()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET lifecycle='reconnect_required' WHERE id=2")
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            self.stop("delete")
        with patch("app.services.source_layout.vault_local_access",
                   return_value=SimpleNamespace(local_operations_allowed=False)):
            status = self.stop("keep")
        self.assertTrue(status["root_released"])
        self.assertEqual(self.count("SELECT 1 FROM jobs WHERE action<>'upload'"), 0)
        self.assert_untouched(current)

    # -- revocation and execution -------------------------------------------

    def test_a_batch_observed_reappearance_revokes_the_whole_group(self) -> None:
        current = self.cloud_only()
        self.second_cloud_only()
        scheduled = self.schedule(paths=["report.txt", "other.txt"])
        self.file.write_bytes(A)
        stat = self.file.stat()
        with self.db() as connection:
            ArchiveCatalog(connection).observe_local_copies_batch(
                vault_id=2, entries=[("report.txt", "regular", stat.st_size, stat.st_mtime_ns,
                                      storage.now_iso())])
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled", "cancelled"])
        self.assertIsNone(self.claim_group(scheduled.job_ids[1]))
        self.assert_untouched(current)

    def test_metadata_restore_revokes_the_consent_and_never_deletes(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        with self.db() as connection:
            record_metadata_restore(connection, storage.now_iso())
        self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
        self.assertEqual(self.lifecycle(), "reconnect_required")
        self.assertIsNone(self.claim_group(scheduled.job_ids[0]))
        self.assert_untouched(current)

    def test_lost_mount_or_replaced_root_never_authorizes_cloud_deletion(self) -> None:
        for case in ("mount", "root"):
            with self.subTest(case=case):
                self.setUp()
                current = self.cloud_only()
                scheduled = self.schedule()
                if case == "mount":
                    with patch("app.services.source_layout.vault_local_access",
                               return_value=SimpleNamespace(local_operations_allowed=False)):
                        self.run_purge(scheduled.job_ids[0])
                else:
                    with self.db() as connection:
                        connection.execute("UPDATE vaults SET root_identity='replaced' WHERE id=2")
                    self.run_purge(scheduled.job_ids[0])
                self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
                self.assert_untouched(current)

    def test_worker_cannot_bypass_seven_days_with_a_corrupted_earlier_run_after(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule(requested_at=storage.now_iso())
        with self.db() as connection:
            connection.execute("UPDATE jobs SET run_after=now() - interval '1 hour' WHERE group_id=%s",
                               (scheduled.group_id,))
        job = self.claim_group(scheduled.job_ids[0])
        if job is not None:
            storage.process_job(job)
            self.assertEqual(self.statuses(scheduled.group_id), ["cancelled"])
        self.assert_untouched(current)

    def test_cancellation_after_claim_never_claims_to_undo_provider_deletion(self) -> None:
        current = self.cloud_only()
        scheduled = self.schedule()
        self.assertIsNotNone(self.claim_group(scheduled.job_ids[0]))
        with self.db() as connection, self.assertRaises(cloud_deletion.CloudDeletionError):
            cloud_deletion.cancel_cloud_deletion(connection, vault_id=2, group_id=scheduled.group_id,
                                                 actor="owner", cancelled_at=storage.now_iso())
        self.assertNotIn("cancelled", self.statuses(scheduled.group_id))
        self.assertEqual(self.file_row()["current_version_id"], current["id"])


if __name__ == "__main__":
    unittest.main()
