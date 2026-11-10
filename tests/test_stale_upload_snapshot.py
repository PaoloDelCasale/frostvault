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
from app.services.upload_snapshot import UploadSnapshot
from tests import test_upload_provenance_worker as base
from tests.postgres import PostgresConnection

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
            connection.execute("UPDATE jobs SET retry_after=NULL WHERE id=%s", (job_id,))
            claimed = ArchiveCatalog(connection).claim_job(
                job_id=job_id, claim_token=uuid.uuid4().hex, claimed_at=storage.now_iso(),
                claim_expires_at=storage._claim_expiry_at(), now=storage.now_iso(),
                restore_due_before=storage.now_iso())
            self.assertIsNotNone(claimed)
            job = connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job_id,)).fetchone()
        return storage.process_job(job)

    def intents(self):
        with PostgresConnection(self.db_path) as connection:
            return connection.execute("SELECT * FROM upload_intents ORDER BY created_at,id").fetchall()

    def change_during_transfer(self, *, foreign_multipart=False):
        """The Local Copy is rewritten while its parts are being sent."""
        def hook(stage, **kwargs):
            if stage == "before_put":
                self.file.write_bytes(B)
                if foreign_multipart:
                    # An incomplete upload this intent never journaled: nobody can
                    # prove it is not the interrupted send (ADR-0025/0029).
                    self.provider.multiparts.append(("docs/report.txt", "untraceable"))
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
        # the Local Copy no longer matches snapshot A: the unsent intent is closed.
        self.run_job(job)
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 2), row["message"])
        self.assertIsNone(row["upload_snapshot_json"])
        self.assertEqual(self.provider.puts, 0)
        closed = self.intents()
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]["send_started"], closed[0]["settled"], closed[0]["archive_version_id"]), (False, True, None))
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
        row = self.row(job)
        snapshot = UploadSnapshot.from_json(row["upload_snapshot_json"])
        self.assertEqual(snapshot.plaintext_sha256, hashlib.sha256(B).hexdigest())
        self.assertEqual(version["upload_snapshot_json"], row["upload_snapshot_json"])
        old, new = self.intents()
        self.assertEqual(old["id"], closed[0]["id"])
        self.assertEqual(old["snapshot_json"], closed[0]["snapshot_json"], "a closed intent is never re-pinned")
        self.assertNotEqual(old["token"], new["token"])
        self.assertEqual((new["original_job_id"], new["snapshot_json"], new["archive_version_id"]),
                         (job, row["upload_snapshot_json"], version["id"]))

    def test_unsent_intent_with_stale_snapshot_is_closed_without_spending_the_budget(self):
        # The send never started: the next attempt builds a new snapshot instead of
        # failing against A until UPLOAD_RETRY_MAX_ATTEMPTS (job.source_changed_stalled).
        job = self.queue()
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
        self.assertIsNone(row["upload_snapshot_json"])
        self.assertIsNone(row["upload_plaintext_sha256"])
        (intent,) = self.intents()
        self.assertEqual((intent["send_started"], intent["settled"]), (False, True))
        self.assertEqual(UploadSnapshot.from_json(intent["snapshot_json"]).plaintext_sha256,
                         hashlib.sha256(A).hexdigest(), "snapshot A stays immutable on its closed intent")
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
        snapshot_a = self.row(job)["upload_snapshot_json"]
        self.provider.hook = lambda *args, **kwargs: None
        # The retry reconciles first: it neither closes the intent nor hashes or
        # sends the changed Local Copy, and the block is durable, not a retry loop.
        with patch.object(storage, "prepare_upload_snapshot", side_effect=AssertionError("hashed")) as hashed:
            self.run_job(job)
            hashed.assert_not_called()
        row = self.row(job)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["upload_snapshot_json"], snapshot_a)
        self.assertEqual((self.provider.puts, self.provider.aborted), (0, []))
        (intent,) = self.intents()
        self.assertEqual((intent["send_started"], intent["settled"], intent["blocked_reason"]), (True, False, "ambiguous_send"))
        with PostgresConnection(self.db_path) as connection:
            self.assertEqual(provenance.load_for_job(connection, job)["id"], intent["id"])


if __name__ == "__main__":
    unittest.main()
