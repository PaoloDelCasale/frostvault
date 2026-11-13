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
    observe_content = fixtures.UploadProvenanceWorkerTests.observe_content

    def interrupted_multipart(self, *, journaled=True, foreign=False):
        """An ambiguous send whose sender left an incomplete multipart upload behind."""
        self.stable_content()
        job = self.fail_before_provider_request(RuntimeError("worker crashed after CreateMultipartUpload"))
        intent = self.intent()
        self.provider.multiparts.append((self.key, "own-upload"))
        if journaled:
            provenance.journal_multipart(storage.db, intent["id"], "own-upload", storage.now_iso())
        if foreign:
            self.provider.multiparts.append((self.key, "foreign-upload"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        return job, max(j["id"] for j in self.jobs())

    def multiparts(self):
        with self.db() as connection:
            return [(r["upload_id"], r["state"]) for r in connection.execute(
                "SELECT upload_id, CASE WHEN closed_at IS NULL THEN 'open' ELSE 'closed' END AS state "
                "FROM upload_multiparts ORDER BY upload_id").fetchall()]

    def test_the_journaled_upload_is_aborted_and_the_upload_is_sent_again(self):
        _original, resumed = self.interrupted_multipart()
        self.assertEqual(self.multiparts(), [("own-upload", "open")])
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.aborted, [(self.key, "own-upload")])
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
        self.assertEqual(self.provider.multiparts, [(self.key, "own-upload")])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        self.assertEqual(self.multiparts(), [])

    def test_a_foreign_upload_beside_the_journaled_one_is_left_alone(self):
        _original, resumed = self.interrupted_multipart(foreign=True)
        self.run_job(resumed)
        self.assertIn("ambiguous_send", self.row(resumed)["message"])
        self.assertEqual(self.provider.aborted, [(self.key, "own-upload")])
        self.assertEqual(self.provider.multiparts, [(self.key, "foreign-upload")])
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
        self.assertIn("abort_permission_denied", row["message"])
        self.assertIn("s3:AbortMultipartUpload", row["message"])
        self.assertEqual(self.multiparts(), [("own-upload", "open")])
        self.assertEqual(self.provider.puts, 0)

    def test_a_complete_whose_response_was_lost_closes_its_row_when_the_version_is_published(self):
        # The multipart upload completed at S3 but the answer never arrived: no receipt,
        # the row stays open until reconciliation finds the version by its token.
        self.stable_content()
        self.provider.hook = lambda stage, **kwargs: (_ for _ in ()).throw(RuntimeError("response lost")) if stage == "accepted" else None
        job = self.queue()
        self.run_job(job)
        self.provider.hook = lambda *args, **kwargs: None
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertEqual(self.provider.puts, 1)
        provenance.journal_multipart(storage.db, self.intent()["id"], "completed-upload", storage.now_iso())
        self.assertEqual(self.multiparts(), [("completed-upload", "open")])
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts, self.provider.aborted), (1, []))
        self.assertEqual(self.multiparts(), [("completed-upload", "closed")])

    def test_settling_an_unstarted_intent_closes_its_rows(self):
        self.stable_content()
        # A committed attempt whose send was never armed (a DB retry between units).
        fixtures.prepare_unsent(self, self.queue())
        intent = self.intent()
        provenance.journal_multipart(storage.db, intent["id"], "refused-abort", storage.now_iso())
        with self.db() as connection:
            provenance._close_abandoned(connection, intent["file_id"], storage.now_iso())
        self.assertEqual(self.multiparts(), [("refused-abort", "closed")])
        self.assertEqual((self.intent()["settled"], self.intent()["outcome"]), (True, "unsent"))

    def test_a_started_unsettled_intent_keeps_its_rows(self):
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        intent = self.intent()
        provenance.journal_multipart(storage.db, intent["id"], "live-obligation", storage.now_iso())
        with self.db() as connection, self.assertRaises(provenance.UploadBlocked) as refused:
            provenance._close_abandoned(connection, intent["file_id"], storage.now_iso())
        self.assertEqual(refused.exception.reason, "ambiguous_send")
        self.assertEqual(self.multiparts(), [("live-obligation", "open")])

    def test_the_journal_is_written_before_the_first_part_and_closed_on_completion(self):
        # The worker's own journal callback, driven the way the facade drives it.
        job = self.queue()
        seen = []
        def upload(client, **kwargs):
            kwargs["on_multipart"]("created", "live-upload")
            with self.db() as connection:
                seen.append(connection.execute("SELECT CASE WHEN closed_at IS NULL THEN 'open' ELSE 'closed' END AS state FROM upload_multiparts WHERE upload_id='live-upload'").fetchone()["state"])
            kwargs["on_multipart"]("closed", "live-upload")
            return self.provider.upload(client, **kwargs)
        with patch("app.storage.upload_file", side_effect=upload):
            self.run_job(job)
        self.assertEqual(seen, ["open"], self.row(job)["message"])
        self.assertEqual(self.multiparts(), [("live-upload", "closed")])
        self.assert_keeper(job)


if __name__ == "__main__":
    unittest.main()
