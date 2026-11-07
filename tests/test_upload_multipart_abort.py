"""#491: the interrupted multipart upload of an intent is aborted exactly, through the real worker.

The UploadId is journaled before any part; an ambiguous send is resumed, the journaled
(and only the journaled) incomplete upload is aborted, absence is proven and the upload is
sent again. TEST_POSTGRES_URL is mandatory (disposable PG16).
"""
import unittest
from unittest.mock import patch

from app import storage
from app.services import upload_provenance as provenance
from tests import test_upload_ambiguous_send_recovery as recovery
from tests import test_upload_provenance_worker as fixtures

KEY = recovery.KEY


class UploadMultipartAbortTests(unittest.TestCase):
    setUp = fixtures.UploadProvenanceWorkerTests.setUp
    queue = fixtures.UploadProvenanceWorkerTests.queue
    run_job = fixtures.UploadProvenanceWorkerTests.run_job
    row = fixtures.UploadProvenanceWorkerTests.row
    assert_keeper = fixtures.UploadProvenanceWorkerTests.assert_keeper
    http_client = fixtures.UploadProvenanceWorkerTests.http_client
    db = recovery.UploadAmbiguousSendRecoveryTests.db
    stable_content = recovery.UploadAmbiguousSendRecoveryTests.stable_content
    fail_before_provider_request = recovery.UploadAmbiguousSendRecoveryTests.fail_before_provider_request
    jobs = recovery.UploadAmbiguousSendRecoveryTests.jobs
    intent = recovery.UploadAmbiguousSendRecoveryTests.intent
    denied = recovery.UploadAmbiguousSendRecoveryTests.denied
    bucket_probe = recovery.UploadAmbiguousSendRecoveryTests.bucket_probe

    def interrupted_multipart(self, *, journaled=True, foreign=False):
        """An ambiguous send whose sender left an incomplete multipart upload behind."""
        self.stable_content()
        job = self.fail_before_provider_request(RuntimeError("worker crashed after CreateMultipartUpload"))
        intent = self.intent()
        self.provider.multiparts.append((KEY, "own-upload"))
        if journaled:
            provenance.journal_multipart(storage.db, intent["id"], "own-upload", storage.now_iso())
        if foreign:
            self.provider.multiparts.append((KEY, "foreign-upload"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        return job, max(j["id"] for j in self.jobs())

    def multiparts(self):
        with self.db() as connection:
            return [(r["upload_id"], r["state"]) for r in connection.execute("SELECT upload_id,state FROM upload_multiparts ORDER BY upload_id").fetchall()]

    def test_the_journaled_upload_is_aborted_and_the_upload_is_sent_again(self):
        _original, resumed = self.interrupted_multipart()
        self.assertEqual(self.multiparts(), [("own-upload", "open")])
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.aborted, [(KEY, "own-upload")])
        self.assertEqual(self.provider.multiparts, [])
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertEqual(self.multiparts(), [("own-upload", "closed")])
        self.assertTrue(self.intent()["settled"])

    def test_an_untraceable_upload_is_never_aborted_and_keeps_the_send_ambiguous(self):
        _original, resumed = self.interrupted_multipart(journaled=False)
        self.run_job(resumed)
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed")
        self.assertIn("ambiguous_send", row["message"])
        self.assertEqual(self.provider.aborted, [])
        self.assertEqual(self.provider.multiparts, [(KEY, "own-upload")])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        self.assertEqual(self.multiparts(), [])

    def test_a_foreign_upload_beside_the_journaled_one_is_left_alone(self):
        _original, resumed = self.interrupted_multipart(foreign=True)
        self.run_job(resumed)
        self.assertIn("ambiguous_send", self.row(resumed)["message"])
        self.assertEqual(self.provider.aborted, [(KEY, "own-upload")])
        self.assertEqual(self.provider.multiparts, [(KEY, "foreign-upload")])
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.multiparts(), [("own-upload", "closed")])

    def test_a_refused_abort_blocks_only_the_file(self):
        from app.services import cloud_preflight_gate
        _original, resumed = self.interrupted_multipart()
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(True), \
                patch.object(self.provider, "abort_multipart_upload", side_effect=self.denied(operation="AbortMultipartUpload")):
            self.run_job(resumed)
        invalidate.assert_not_called()
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed", row["message"])
        self.assertIn("write_permission_denied", row["message"])
        self.assertIn("s3:AbortMultipartUpload", row["message"])
        self.assertEqual(self.multiparts(), [("own-upload", "open")])
        self.assertEqual(self.provider.puts, 0)

    def test_the_journal_is_written_before_the_first_part_and_closed_on_completion(self):
        # The worker's own journal callback, driven the way the facade drives it.
        job = self.queue()
        seen = []
        def upload(client, **kwargs):
            kwargs["on_multipart"]("created", "live-upload")
            with self.db() as connection:
                seen.append(connection.execute("SELECT state FROM upload_multiparts WHERE upload_id='live-upload'").fetchone()["state"])
            kwargs["on_multipart"]("closed", "live-upload")
            return self.provider.upload(client, **kwargs)
        with patch("app.storage.upload_file", side_effect=upload):
            self.run_job(job)
        self.assertEqual(seen, ["open"])
        self.assertEqual(self.multiparts(), [("live-upload", "closed")])
        self.assert_keeper(job)


if __name__ == "__main__":
    unittest.main()
