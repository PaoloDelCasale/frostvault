"""#538: a source change during an upload never pins the Job to a stale snapshot.

Real admission/claim/process_job on disposable PG16 (TEST_POSTGRES_URL is
mandatory), with the persistent versioned provider of the #464 suite.
"""
import hashlib
import unittest
import uuid
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog
from app.services import upload_provenance as provenance
from tests import test_upload_provenance_worker as base
from tests.postgres import PostgresConnection
from tests.upload_support import claimed_job_row

A, B = base.A, base.B
CHANGED = "Local file changed since fingerprinting"


class StaleUploadSnapshotTests(unittest.TestCase):
    setUp = base.UploadProvenanceWorkerTests.setUp
    queue = base.UploadProvenanceWorkerTests.queue
    row = base.UploadProvenanceWorkerTests.row
    assert_keeper = base.UploadProvenanceWorkerTests.assert_keeper

    def run_job(self, job_id):
        # The retry delay is the Vault stability window: make the Job due now.
        with PostgresConnection(self.db_path) as connection:
            connection.execute("UPDATE jobs SET run_after=NULL WHERE id=%s", (job_id,))
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = claimed_job_row(connection, job_id, claimed)
        return storage.process_job(job)

    def intents(self):
        """Every upload attempt with its snapshot (the former intents)."""
        with PostgresConnection(self.db_path) as connection:
            return connection.execute(
                """SELECT a.*, a.send_started_at IS NOT NULL AS send_started,
                          a.settled_at IS NOT NULL AS settled, s.closed_at AS snapshot_closed_at,
                          encode(s.plaintext_sha256, 'hex') AS snapshot_sha256
                   FROM upload_attempts a JOIN upload_snapshots s ON s.id=a.snapshot_id
                   ORDER BY a.created_at, a.send_started_at NULLS LAST""").fetchall()

    def bound(self, job_id):
        """The snapshot the Job is bound to, or None once it forgot it."""
        with PostgresConnection(self.db_path) as connection:
            return connection.execute(
                """SELECT s.*, encode(s.plaintext_sha256, 'hex') AS sha256 FROM job_uploads ju
                   JOIN upload_snapshots s ON s.id=ju.snapshot_id WHERE ju.job_id=%s""",
                (job_id,)).fetchone()

    def change_during_transfer(self, *, foreign_multipart=False):
        """The Local Copy is rewritten while its parts are being sent."""
        def hook(stage, **kwargs):
            if stage == "before_put":
                self.file.write_bytes(B)
                if foreign_multipart:
                    # An incomplete upload this intent never journaled: nobody can
                    # prove it is not the interrupted send (ADR-0025/0029).
                    self.provider.multiparts.append((self.key, "untraceable"))
                raise RuntimeError(CHANGED)
        return hook

    def test_change_during_transfer_then_stable_uploads_a_fresh_snapshot(self):
        self.provider.hook = self.change_during_transfer()
        job = self.queue()
        self.run_job(job)
        self.assertEqual((self.row(job)["status"], self.row(job)["retry_count"]), ("retrying", 1))
        first = self.intents()
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["send_started"], "the interrupted send is reconciled first")
        self.provider.hook = lambda *args, **kwargs: None

        # Retry 1 reconciles the started send (proven absent, no PUT), then finds
        # the Local Copy no longer matches snapshot A: snapshot A is closed.
        self.run_job(job)
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 2), row["message"])
        self.assertIsNone(self.bound(job))
        self.assertEqual(self.provider.puts, 0)
        closed = self.intents()
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]["send_started"], closed[0]["settled"], closed[0]["landed_version_id"]), (True, True, None))
        self.assertIsNotNone(closed[0]["snapshot_closed_at"])
        with PostgresConnection(self.db_path) as connection:
            self.assertTrue(provenance.proven_absent(connection, closed[0]["id"]))
            self.assertIsNone(provenance.load_for_job(connection, job))
            self.assertIsNone(provenance.outstanding(connection, self.file_id))

        # Retry 2 fingerprints the stable content B and uploads it.
        self.run_job(job)
        version = self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)
        stored, _metadata = self.provider.objects[version["object_key"], version["provider_version_id"]]
        self.assertEqual(stored, B)
        bound = self.bound(job)
        self.assertEqual(bound["sha256"], hashlib.sha256(B).hexdigest())
        self.assertEqual(bytes(version["plaintext_sha256"]).hex(), bound["sha256"])
        old, new = self.intents()
        self.assertEqual(old["id"], closed[0]["id"])
        self.assertEqual((old["snapshot_id"], old["snapshot_sha256"]), (closed[0]["snapshot_id"], closed[0]["snapshot_sha256"]),
                         "a closed snapshot is never re-pinned")
        self.assertNotEqual(old["snapshot_id"], new["snapshot_id"])
        self.assertNotEqual(old["token"], new["token"])
        self.assertEqual((new["job_id"], new["snapshot_id"], new["landed_version_id"]),
                         (job, bound["id"], version["provider_version_id"]))

    def test_unsent_intent_with_stale_snapshot_is_closed_without_spending_the_budget(self):
        # The send never started: the next attempt builds a new snapshot instead of
        # failing against A until UPLOAD_RETRY_MAX_ATTEMPTS (job.source_changed_stalled).
        job = self.queue()
        base.prepare_unsent(self, job)
        with PostgresConnection(self.db_path) as connection:
            # One retry left: a single stale comparison would fail the Job.
            connection.execute("UPDATE jobs SET retry_count=%s WHERE id=%s", (storage.UPLOAD_RETRY_MAX_ATTEMPTS - 1, job))
        def changed_before_send(*args, **kwargs):
            self.file.write_bytes(B)
            raise RuntimeError(CHANGED)
        with patch.object(provenance, "mark_send_started", side_effect=changed_before_send):
            self.run_job(job)
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", storage.UPLOAD_RETRY_MAX_ATTEMPTS))
        self.assertIsNone(self.bound(job))
        (intent,) = self.intents()
        self.assertEqual((intent["send_started"], intent["settled"], intent["outcome"]), (False, True, "unsent"))
        self.assertIsNotNone(intent["snapshot_closed_at"])
        self.assertEqual(intent["snapshot_sha256"], hashlib.sha256(A).hexdigest(),
                         "snapshot A stays immutable once closed")
        with PostgresConnection(self.db_path) as connection:
            self.assertIsNone(provenance.load_for_job(connection, job))
        self.run_job(job)
        version = self.assert_keeper(job)
        self.assertEqual(self.provider.objects[version["object_key"], version["provider_version_id"]][0], B)
        self.assertEqual(self.provider.puts, 1)

    def test_started_send_that_cannot_be_proven_absent_keeps_reconciliation(self):
        self.provider.hook = self.change_during_transfer(foreign_multipart=True)
        job = self.queue()
        self.run_job(job)
        self.assertEqual(self.row(job)["status"], "retrying")
        snapshot_a = self.bound(job)["id"]
        self.provider.hook = lambda *args, **kwargs: None
        # The retry reconciles first: it neither closes the intent nor hashes or
        # sends the changed Local Copy, and the block is durable, not a retry loop.
        with patch.object(storage, "prepare_upload_snapshot", side_effect=AssertionError("hashed")) as hashed:
            self.run_job(job)
            hashed.assert_not_called()
        row = self.row(job)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.bound(job)["id"], snapshot_a)
        self.assertEqual((self.provider.puts, self.provider.aborted), (0, []))
        (intent,) = self.intents()
        self.assertEqual((intent["send_started"], intent["settled"]), (True, False))
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.describe(connection, vault_id=2, path="report.txt")["reason"], "ambiguous_send")
            self.assertEqual(provenance.load_for_job(connection, job).id, str(intent["id"]))


if __name__ == "__main__":
    unittest.main()
