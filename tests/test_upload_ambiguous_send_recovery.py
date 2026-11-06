"""#484/#486: recovery from interrupted sends through the real admission/claim/worker.

A send that never reached S3 (database or network lost around the PUT) must not
leave a file in ``ambiguous_send`` forever, and a transient condition must not be
reported as ``identity_changed``. TEST_POSTGRES_URL is mandatory (disposable PG16).
"""
import unittest
from unittest.mock import patch

import psycopg

from app import storage
from app.services import notifications, upload_provenance as provenance
from app.services import upload_remediation as remediation
from app.services.vault_relocation import VaultRelocationError
from tests import test_upload_provenance_worker as fixtures
from tests.postgres import PostgresConnection

KEY = "docs/report.txt"


class UploadAmbiguousSendRecoveryTests(unittest.TestCase):
    setUp = fixtures.UploadProvenanceWorkerTests.setUp
    queue = fixtures.UploadProvenanceWorkerTests.queue
    run_job = fixtures.UploadProvenanceWorkerTests.run_job
    row = fixtures.UploadProvenanceWorkerTests.row
    assert_keeper = fixtures.UploadProvenanceWorkerTests.assert_keeper
    http_client = fixtures.UploadProvenanceWorkerTests.http_client

    def db(self):
        return PostgresConnection(self.db_path)

    def stable_content(self):
        # Content stability is exercised elsewhere; here the file is old enough.
        self.enterContext(patch("app.storage._automatic_upload_ready", return_value=True))

    def retry_is_due(self, job):
        with self.db() as connection:
            connection.execute("UPDATE jobs SET retry_after='2000-01-01T00:00:00+00:00' WHERE id=%s", (job,))

    def fail_before_provider_request(self, error):
        """Send is committed, then the process loses its dependency before any request."""
        def fault(stage, **kwargs):
            if stage == "before_put":
                raise error
        self.provider.hook = fault
        job = self.queue()
        self.run_job(job)
        self.provider.hook = lambda *args, **kwargs: None
        return job

    def queue_fresh(self):
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE status NOT IN ('failed','completed','cancelled')")
        return self.queue()

    def jobs(self):
        with self.db() as connection:
            return connection.execute("SELECT id,status,upload_reconcile_only,message FROM jobs ORDER BY id").fetchall()

    def notification_events(self):
        with self.db() as connection:
            return connection.execute("SELECT event,problem_status FROM notifications ORDER BY id").fetchall()

    def intent(self):
        with self.db() as connection:
            return connection.execute("SELECT * FROM upload_intents").fetchone()

    def test_database_connection_loss_retries_the_job_and_absence_restarts_the_upload(self):
        job = self.fail_before_provider_request(psycopg.OperationalError("connection is bad: Network is unreachable"))
        row = self.row(job)
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 1), row["message"])
        self.assertTrue(self.intent()["send_started"])
        self.assertEqual(self.provider.puts, 0)
        self.retry_is_due(job)
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        self.assertTrue(self.intent()["settled"])
        self.assertEqual([j["status"] for j in self.jobs()], ["completed"])
        self.assertNotIn("job_failed", [n["event"] for n in self.notification_events()])

    def test_blocked_remediation_without_any_s3_object_recovers_with_retry_failed(self):
        self.stable_content()
        job = self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.row(job)["status"], "failed")
        self.assertTrue(self.intent()["send_started"])
        client = self.http_client()
        response = client.post("/api/upload/retry-failed?vault_id=2")
        self.assertEqual((response.status_code, response.json()), (202, {"item_count": 1}))
        resumed = max(j["id"] for j in self.jobs())
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT state FROM upload_remediations").fetchone()["state"], "resolved")
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_receipts WHERE response_json LIKE %s", ("%NoWriteOperation%",)).fetchone()["n"], 1)
        self.assertEqual([j["status"] for j in self.jobs()], ["failed", "completed"])
        self.assertTrue(self.intent()["settled"])
        self.assertEqual(client.post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 0})

    def test_network_loss_while_reconciling_keeps_the_item_in_verification_not_failed(self):
        # #486: an unreadable S3 inventory is neither a changed inventory nor a failure.
        from botocore.exceptions import EndpointConnectionError
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        with patch.object(self.provider, "list_object_versions",
                          side_effect=EndpointConnectionError(endpoint_url="https://s3.invalid")):
            self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "retrying")
        with self.db() as connection:
            episode = connection.execute("SELECT state,phase,reason FROM upload_remediations").fetchone()
            self.assertEqual((episode["state"], episode["phase"]), ("retrying", "verification"))
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
        self.assertEqual([n["event"] for n in self.notification_events()].count("upload_reconciliation"), 0)
        self.retry_is_due(resumed)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_a_listed_incomplete_multipart_keeps_the_send_ambiguous_and_never_puts(self):
        job = self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.provider.multiparts.append((KEY, "in-flight-upload"))
        client = self.http_client()
        self.assertEqual(client.post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        self.run_job(resumed)
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed")
        self.assertIn("ambiguous_send", row["message"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        intent = self.intent()
        self.assertTrue(intent["send_started"])
        self.assertEqual(intent["blocked_reason"], "ambiguous_send")
        self.assertEqual(self.row(job)["status"], "failed")

    def test_reconcile_names_the_real_reason_instead_of_identity_changed_for_an_active_job(self):
        job = self.fail_before_provider_request(RuntimeError("timeout talking to the database"))
        self.assertEqual(self.row(job)["status"], "retrying")
        response = self.http_client().post("/api/upload/reconcile", json={"path": "report.txt", "is_directory": False})
        self.assertEqual(response.status_code, 409)
        self.assertIn("active_operation", response.json()["detail"])
        self.assertNotIn("identity_changed", response.json()["detail"])
        self.assertEqual(len(self.jobs()), 1)

    def test_reconcile_of_a_suspended_remediation_names_its_stored_reason(self):
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        with self.db() as connection:
            connection.execute("""INSERT INTO vault_component_health(vault_id,component,last_error,updated_at)
                VALUES (2,'source','mount lost',%s) ON CONFLICT (vault_id,component)
                DO UPDATE SET last_error=excluded.last_error""", (storage.now_iso(),))
        response = self.http_client().post("/api/upload/reconcile", json={"path": "report.txt", "is_directory": False})
        self.assertEqual(response.status_code, 409)
        self.assertIn("source_volume_unavailable", response.json()["detail"])
        self.assertNotIn("identity_changed", response.json()["detail"])

    def test_transient_source_check_failure_waits_instead_of_failing_with_identity_changed(self):
        # #486: a hiccup reading the enrolled root is not an identity change.
        job = self.queue()
        with patch("app.storage.vault_relocation.root_identity", side_effect=VaultRelocationError("inaccessible", "transient stat failure")):
            self.assertFalse(self.run_job(job))
        row = self.row(job)
        self.assertEqual(row["status"], "queued", row["message"])
        self.assertEqual(self.notification_events(), [])
        self.assertEqual(self.provider.puts, 0)
        self.run_job(job)
        self.assert_keeper(job)

    def test_transient_source_check_failure_after_send_started_recovers_without_failure(self):
        # The check inside the transfer wrapper runs after the send was journaled.
        job = self.queue()
        original = storage._require_upload_source_identity
        calls = []
        def flaky(candidate):
            calls.append(1)
            if len(calls) == 2:
                raise storage.SourceVolumeUnavailable("Source Volume identity could not be read")
            return original(candidate)
        with patch("app.storage._require_upload_source_identity", side_effect=flaky):
            self.assertFalse(self.run_job(job))
        row = self.row(job)
        self.assertEqual(row["status"], "queued", row["message"])
        self.assertTrue(self.intent()["send_started"])
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.notification_events(), [])
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)

    def test_an_observed_root_replacement_still_fails_as_identity_changed(self):
        for name, patched in (("mismatch", {"return_value": "another-root"}),
                              ("symlink", {"side_effect": VaultRelocationError("symlink", "symlinked root")})):
            with self.subTest(case=name):
                job = self.queue_fresh()
                with patch("app.storage.vault_relocation.root_identity", **patched):
                    self.run_job(job)
                row = self.row(job)
                self.assertEqual(row["status"], "failed")
                self.assertIn("identity_changed", row["message"])
                self.assertEqual(self.provider.puts, 0)


if __name__ == "__main__":
    unittest.main()
