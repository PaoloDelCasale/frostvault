"""Recovery and free-space on the rebuilt model (#594, ADR-0035).

Real admission, claim and ``storage.process_job`` on a disposable PostgreSQL 16
database; the S3 transport is the synthetic ``VersionedProvider`` of
``test_upload_worker`` (exact VersionIds, counted PUT/GET/HEAD). Current is
always produced by a real upload Job first.

Covered: a recovery reads only the exact Current and publishes the Local Copy
observation, its digest and ``recovered_at`` in one compare-and-swap; it never
overwrites an entry that appeared at the path and never overwrites a newer
observation; an archived class is restored once; restart reconciliation adopts
only the exact proof, without downloading; free-space removes only the
fingerprinted bytes of a verified Current, and new bytes never inherit the
released copy's fingerprint or match.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B, sha

ORIGINAL_MTIME_NS = 1_700_000_000_123_456_789


class RecoveryWorkerTests(unittest.TestCase):
    setUp = fixtures.UploadWorkerTests.setUp
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    job = fixtures.UploadWorkerTests.job
    file_row = fixtures.UploadWorkerTests.file_row
    rows = fixtures.UploadWorkerTests.rows
    version = fixtures.UploadWorkerTests.version
    assert_current = fixtures.UploadWorkerTests.assert_current

    # -- helpers -------------------------------------------------------------

    def protected(self, payload: bytes = A) -> dict:
        """Upload ``payload`` for real (with its original mtime) and return Current."""
        self.write(payload, mtime_ns=ORIGINAL_MTIME_NS)
        job_id = self.queue()
        self.run_job(job_id)
        return self.assert_current(job_id, payload)

    def lose_local_copy(self) -> None:
        self.file.unlink()
        with self.db() as connection:
            ArchiveCatalog(connection).mark_local_copy_missing(
                self.file_id, observed_at=storage.now_iso())

    def admit(self, action: str) -> int:
        with self.db() as connection:
            job_ids, _bytes, eligible = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action=action, requested_at=storage.now_iso(),
                group_id=str(uuid.uuid4()), is_directory=False)
        self.assertEqual((len(job_ids), eligible), (1, 1), f"{action} must be admitted")
        return job_ids[0]

    def expire(self, job_id: int, status: str) -> None:
        """Leave the Job as a crashed worker would: held, with an expired lease."""
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job_id))
            connection.execute(
                """INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                   VALUES (%s, %s, now() - interval '2 hours', now() - interval '1 hour')
                   ON CONFLICT (job_id) DO UPDATE SET claimed_at=EXCLUDED.claimed_at,
                       expires_at=EXCLUDED.expires_at""",
                (job_id, str(uuid.uuid4())))

    def assert_recovered(self, row: dict, current: dict) -> None:
        self.assertEqual(row["local_presence"], "present")
        self.assertEqual(bytes(row["local_sha256"]), bytes(current["plaintext_sha256"]))
        self.assertEqual(row["matched_version_id"], current["id"])
        self.assertIsNone(row["held_version_id"])
        self.assertIsNotNone(row["local_recovered_at"])
        self.assertEqual(row["local_recovered_at"], row["local_observed_at"])
        stat = self.file.stat()
        self.assertEqual((row["local_size"], row["local_mtime_ns"]),
                         (stat.st_size, stat.st_mtime_ns))

    # -- recovery ------------------------------------------------------------

    def test_recovery_publishes_observation_digest_and_recovered_at_together(self) -> None:
        current = self.protected()
        self.lose_local_copy()
        before = self.file_row()["generation"]
        job_id = self.admit("recover")
        self.assertEqual(str(self.job(job_id)["archive_version_id"]), str(current["id"]))
        self.assertTrue(self.run_job(job_id))
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id)["message"])
        self.assertEqual(self.file.read_bytes(), A)
        self.assertEqual(self.file.stat().st_mtime_ns, ORIGINAL_MTIME_NS)
        row = self.file_row()
        self.assert_recovered(row, current)
        self.assertEqual(row["generation"], before + 1, "one observation, one CAS")
        self.assertEqual((self.provider.puts, self.provider.gets), (1, 1))
        self.assertEqual(list(self.source.iterdir()), [self.file], "no temporary residue")

    def test_recovered_copy_is_not_uploaded_again(self) -> None:
        self.protected()
        self.lose_local_copy()
        self.run_job(self.admit("recover"))
        with self.db() as connection:
            job_ids, _bytes, eligible = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action="upload", requested_at=storage.now_iso(),
                group_id=str(uuid.uuid4()), is_directory=False)
        self.assertEqual((job_ids, eligible), ([], 0), "matched to Current: nothing to upload")

    def test_an_entry_that_appears_at_the_path_is_never_overwritten(self) -> None:
        self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        previous = self.provider.hook

        def intruder(stage, **kwargs):
            previous(stage, **kwargs)
            if stage == "get":
                self.file.write_bytes(B)
        self.provider.hook = intruder
        generation = self.file_row()["generation"]
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("already exists", job["message"])
        self.assertEqual(self.file.read_bytes(), B, "the foreign entry is preserved")
        row = self.file_row()
        self.assertEqual((row["generation"], row["local_presence"], row["local_recovered_at"]),
                         (generation, "missing", None), "nothing was published for it")
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["report.txt"])

    def test_a_newer_observation_wins_over_the_recovery_publication(self) -> None:
        self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        publish = storage._publish_without_replacing

        def observed_meanwhile(temporary, destination):
            publish(temporary, destination)
            # Another observer saw the path before the recovery's CAS.
            with self.db() as connection:
                self.observe(connection)
        with patch("app.storage._publish_without_replacing", side_effect=observed_meanwhile):
            self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("newer observation", job["message"])
        row = self.file_row()
        self.assertEqual(row["local_presence"], "present")
        self.assertIsNone(row["local_recovered_at"], "the newer observation is preserved")
        self.assertIsNone(row["matched_version_id"])
        self.assertIsNotNone(row["held_version_id"], "a reappearance is held, not matched")
        self.assertEqual(self.file.read_bytes(), A, "the recovered bytes are never removed")

    def test_only_current_is_recovered_and_matched(self) -> None:
        self.protected(A)
        replaced = self.protected(B)
        self.lose_local_copy()
        job_id = self.admit("recover")
        self.assertEqual(str(self.job(job_id)["archive_version_id"]), str(replaced["id"]))
        self.run_job(job_id)
        self.assertEqual(self.file.read_bytes(), B)
        self.assertEqual(self.file_row()["matched_version_id"], replaced["id"])

    def test_a_present_local_copy_is_never_recovered_over(self) -> None:
        self.protected()
        with self.db() as connection:
            job_ids, _bytes, eligible = ArchiveCatalog(connection).queue_jobs(
                vault_id=2, path="report.txt", action="recover", requested_at=storage.now_iso(),
                group_id=str(uuid.uuid4()), is_directory=False)
        self.assertEqual((job_ids, eligible), ([], 0))

    # -- archived classes: one RestoreObject, then the exact version --------

    def test_archived_version_is_restored_once_then_recovered(self) -> None:
        current = self.protected()
        self.lose_local_copy()
        with self.db() as connection:
            connection.execute("UPDATE archive_versions SET storage_class='DEEP_ARCHIVE' WHERE id=%s",
                               (current["id"],))
        restore = {"header": None, "requests": []}
        head = self.provider.head_object

        def archived_head(**kwargs):
            response = head(**kwargs)
            response["StorageClass"] = "DEEP_ARCHIVE"
            if restore["header"]:
                response["Restore"] = restore["header"]
            return response

        def restore_object(**kwargs):
            restore["requests"].append(kwargs)
            restore["header"] = 'ongoing-request="true"'
        self.provider.head_object = archived_head
        self.provider.restore_object = restore_object
        job_id = self.admit("recover")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "restoring")
        self.assertEqual(len(restore["requests"]), 1)
        self.assertEqual(restore["requests"][0]["VersionId"], current["provider_version_id"])
        self.assertEqual(self.version(current["id"])["restore_state"], "restoring")
        self.assertEqual(self.provider.gets, 0)

        def poll() -> None:
            with self.db() as connection:
                connection.execute(
                    "UPDATE jobs SET updated_at=now() - interval '1 day' WHERE id=%s", (job_id,))
            with patch("app.storage._restore_due_before", return_value=storage.now_iso()):
                self.run_job(job_id)
        poll()
        self.assertEqual(self.job(job_id)["status"], "restoring")
        self.assertEqual(len(restore["requests"]), 1, "RestoreObject is never requested twice")
        restore["header"] = 'ongoing-request="false", expiry-date="Fri, 21 Dec 2029 00:00:00 GMT"'
        poll()
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id)["message"])
        self.assertEqual(len(restore["requests"]), 1)
        version = self.version(current["id"])
        self.assertEqual(version["restore_state"], "available")
        self.assertEqual(version["restore_expiry"], datetime(2029, 12, 21, tzinfo=timezone.utc))
        self.assert_recovered(self.file_row(), current)

    # -- restart reconciliation: only the exact proof ------------------------

    def test_restart_adopts_the_exact_destination_without_downloading(self) -> None:
        current = self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        self.file.write_bytes(A)  # the crashed worker had published the bytes
        self.expire(job_id, "verifying")
        storage.reconcile_interrupted_jobs()
        job = self.job(job_id)
        self.assertEqual(job["status"], "completed", job["message"])
        self.assert_recovered(self.file_row(), current)
        self.assertEqual(self.provider.gets, 0, "nothing is downloaded to adopt the proof")

    def test_restart_preserves_a_destination_with_other_bytes(self) -> None:
        self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        self.file.write_bytes(B)
        generation = self.file_row()["generation"]
        self.expire(job_id, "downloading")
        storage.reconcile_interrupted_jobs()
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("does not match", job["message"])
        self.assertEqual(self.file.read_bytes(), B)
        row = self.file_row()
        self.assertEqual((row["generation"], row["local_presence"]), (generation, "missing"))

    def test_restart_requeues_an_absent_destination_and_downloads_once(self) -> None:
        current = self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        residue = self.source / f".report.txt.restore-{uuid.uuid4().hex}.tmp"
        residue.write_bytes(A[:3])
        self.expire(job_id, "downloading")
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "queued")
        self.assertFalse(residue.exists(), "only the application's own residue is removed")
        self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.provider.gets, 1)
        self.assert_recovered(self.file_row(), current)

    def test_restart_removes_sdk_partial_restore_residue_too(self) -> None:
        # Ported from test_storage_cleanup (mocked SQL): the SDK's
        # ``.tmp.<random>.partial`` sibling of an owned restore temporary is
        # residue as well; the absent destination is requeued.
        self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        token = uuid.uuid4().hex
        residue = self.source / f".report.txt.restore-{token}.tmp"
        partial = self.source / f".report.txt.restore-{token}.tmp.random.partial"
        residue.write_bytes(A[:3])
        partial.write_bytes(A[:2])
        self.expire(job_id, "downloading")
        self.assertEqual(storage.reconcile_interrupted_jobs(),
                         {"completed": 0, "requeued": 1, "failed": 0})
        self.assertEqual(self.job(job_id)["status"], "queued")
        self.assertFalse(residue.exists())
        self.assertFalse(partial.exists())

    def test_restart_requeues_an_interrupted_upload_without_a_send(self) -> None:
        # Ported from test_storage_cleanup (mocked SQL): an upload that died
        # before any send is resumed, not failed.
        self.write(A)
        job_id = self.queue()
        self.expire(job_id, "uploading")
        self.assertEqual(storage.reconcile_interrupted_jobs(),
                         {"completed": 0, "requeued": 1, "failed": 0})
        self.assertEqual(self.job(job_id)["status"], "queued")
        self.run_job(job_id)
        self.assert_current(job_id, A)

    def test_restart_does_not_adopt_a_version_that_is_no_longer_current(self) -> None:
        self.protected(A)
        self.lose_local_copy()
        job_id = self.admit("recover")
        with self.db() as connection:
            # Current lost (deliberate cloud deletion); the Job's pin is stale.
            connection.execute(
                "UPDATE files SET current_version_id=NULL, matched_version_id=NULL WHERE id=%s",
                (self.file_id,))
        self.file.write_bytes(A)
        self.expire(job_id, "verifying")
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "failed")
        self.assertIsNone(self.file_row()["local_recovered_at"])
        self.assertEqual(self.file.read_bytes(), A)

    def test_restart_of_a_live_worker_changes_nothing(self) -> None:
        self.protected()
        self.lose_local_copy()
        job_id = self.admit("recover")
        self.file.write_bytes(A)
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='verifying' WHERE id=%s", (job_id,))
            connection.execute(
                """INSERT INTO job_leases(job_id, token, claimed_at, expires_at)
                   VALUES (%s, %s, now(), now() + interval '1 hour')""", (job_id, str(uuid.uuid4())))
        generation = self.file_row()["generation"]
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "verifying")
        self.assertEqual(self.file_row()["generation"], generation)

    # -- free-space ----------------------------------------------------------

    def fingerprinted(self) -> dict:
        current = self.protected()
        row = self.file_row()
        self.assertEqual(row["matched_version_id"], current["id"])
        return current

    def test_free_space_removes_only_the_matched_local_copy(self) -> None:
        self.fingerprinted()
        job_id = self.admit("free-space")
        with patch("app.storage.local_release_is_confirmed", return_value=True):
            self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id)["message"])
        self.assertFalse(self.file.exists())
        row = self.file_row()
        self.assertEqual(row["local_presence"], "missing")
        self.assertIsNotNone(row["current_version_id"], "Current is untouched")
        self.assertEqual(self.provider.deletes, 0)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_free_space_refuses_new_bytes_and_republishes_their_fingerprint(self) -> None:
        current = self.fingerprinted()
        job_id = self.admit("free-space")
        stat = self.file.stat()
        # Same size and mtime, different bytes: the catalog's fingerprint is stale.
        self.file.write_bytes(B.ljust(len(A), b"!")[:len(A)])
        os.utime(self.file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with patch("app.storage.local_release_is_confirmed", return_value=True):
            self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("changed since fingerprinting", job["message"])
        self.assertTrue(self.file.exists(), "the new bytes are restored, never removed")
        row = self.file_row()
        self.assertEqual(bytes(row["local_sha256"]), sha(self.file.read_bytes()))
        self.assertIsNone(row["matched_version_id"], "new bytes never inherit the match")
        self.assertNotEqual(bytes(row["local_sha256"]), bytes(current["plaintext_sha256"]))
        self.assertEqual([p.name for p in self.source.iterdir()], ["report.txt"])

    def test_free_space_requires_the_release_to_be_confirmed(self) -> None:
        self.fingerprinted()
        job_id = self.admit("free-space")
        with patch("app.storage.local_release_is_confirmed", return_value=False):
            self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "failed")
        self.assertTrue(self.file.exists())

    def test_free_space_refuses_when_current_changed_after_admission(self) -> None:
        self.fingerprinted()
        job_id = self.admit("free-space")
        with self.db() as connection:
            connection.execute(
                "UPDATE files SET current_version_id=NULL, matched_version_id=NULL WHERE id=%s",
                (self.file_id,))
        with patch("app.storage.local_release_is_confirmed", return_value=True):
            self.run_job(job_id)
        self.assertEqual(self.job(job_id)["status"], "failed")
        self.assertTrue(self.file.exists())

    def test_restart_completes_a_free_space_whose_unlink_finished(self) -> None:
        self.fingerprinted()
        job_id = self.admit("free-space")
        self.file.unlink()
        self.expire(job_id, "cleaning")
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "completed")
        self.assertEqual(self.file_row()["local_presence"], "missing")

    def test_restart_restores_a_free_space_claim_and_requeues(self) -> None:
        self.fingerprinted()
        job_id = self.admit("free-space")
        claim = self.source / f".report.txt.cleanup-{uuid.uuid4().hex}.tmp"
        self.file.rename(claim)
        self.expire(job_id, "cleaning")
        storage.reconcile_interrupted_jobs()
        self.assertEqual(self.job(job_id)["status"], "queued")
        self.assertEqual(self.file.read_bytes(), A)
        self.assertFalse(claim.exists())


if __name__ == "__main__":
    unittest.main()
