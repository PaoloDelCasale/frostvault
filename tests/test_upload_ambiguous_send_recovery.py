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
        # Absence is proven, then the database drops once more before the send.
        with patch.object(provenance, "mark_send_started", side_effect=psycopg.OperationalError("connection is bad")):
            self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "retrying")
        self.assertFalse(self.intent()["send_started"])
        with self.db() as connection:
            episode = connection.execute("SELECT state,resolved_by_job_id,current_job_id FROM upload_remediations").fetchone()
            # The file is not protected yet: the episode stays open and linked.
            self.assertEqual((episode["state"], episode["resolved_by_job_id"], episode["current_job_id"]), ("retrying", None, resumed))
            failed = connection.execute("SELECT problem_status FROM notifications WHERE event='job_failed' AND job_id=%s", (job,)).fetchone()
            self.assertEqual(failed["problem_status"], "open")
        self.retry_is_due(resumed)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual((self.provider.puts, self.provider.deletes), (1, 0))
        with self.db() as connection:
            episode = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((episode["state"], episode["phase"], episode["resolved_by_job_id"]), ("resolved", "completed", resumed))
            self.assertIsNotNone(episode["cleanup_inventory_job_id"])
            self.assertEqual(connection.execute("SELECT completed_at IS NOT NULL done FROM snapshot_cleanup_inventories WHERE job_id=%s", (episode["cleanup_inventory_job_id"],)).fetchone()["done"], True)
            notifications.reconcile_problem_states(connection)
            self.assertEqual(connection.execute("SELECT problem_status FROM notifications WHERE event='job_failed' AND job_id=%s", (job,)).fetchone()["problem_status"], "resolved")
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

    def denied(self, code="AccessDenied", status=403, operation="ListObjectVersions"):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": "provider text"},
                            "ResponseMetadata": {"HTTPStatusCode": status}}, operation)

    def blocked_resume(self, patched):
        """Resume an ambiguous send while one provider listing is refused."""
        from app.services import cloud_preflight_gate
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, patched:
            self.run_job(resumed)
        invalidate.assert_not_called()
        return resumed

    def assert_listing_denied_blocks_only_the_file(self, resumed):
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed", row["message"])
        self.assertIn("list_permission_denied", row["message"])
        self.assertIn("s3:ListBucketMultipartUploads", row["message"])
        self.assertIn("s3:ListBucketVersions", row["message"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        self.assertTrue(self.intent()["send_started"])
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT reason FROM snapshot_reconciliation_blocks").fetchone()["reason"], "list_permission_denied")
            self.assertIsNone(connection.execute("SELECT 1 FROM notifications WHERE event='cloud_preflight_blocked'").fetchone())
        self.assertNotIn("S3 readiness", row["message"])

    def test_access_denied_on_multipart_listing_blocks_the_file_and_never_touches_the_gate(self):
        resumed = self.blocked_resume(patch.object(self.provider, "list_multipart_uploads", side_effect=self.denied(operation="ListMultipartUploads")))
        self.assert_listing_denied_blocks_only_the_file(resumed)

    def test_access_denied_on_version_listing_blocks_the_file_and_never_touches_the_gate(self):
        resumed = self.blocked_resume(patch.object(self.provider, "list_object_versions", side_effect=self.denied()))
        self.assert_listing_denied_blocks_only_the_file(resumed)

    def test_credential_errors_on_a_listing_still_use_the_readiness_gate(self):
        from app.services import cloud_preflight_gate
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.http_client().post("/api/upload/retry-failed?vault_id=2")
        resumed = max(j["id"] for j in self.jobs())
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, \
                patch.object(self.provider, "list_object_versions", side_effect=self.denied("InvalidAccessKeyId", 403)):
            self.run_job(resumed)
        invalidate.assert_called_once()
        self.assertNotEqual(self.row(resumed)["status"], "failed")

    def test_provider_5xx_on_a_listing_is_retried_not_reported_as_a_changed_inventory(self):
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.http_client().post("/api/upload/retry-failed?vault_id=2")
        resumed = max(j["id"] for j in self.jobs())
        with patch.object(self.provider, "list_object_versions", side_effect=self.denied("InternalError", 500)):
            self.run_job(resumed)
        row = self.row(resumed)
        self.assertEqual(row["status"], "retrying", row["message"])
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
        self.assertNotIn("upload_reconciliation", [n["event"] for n in self.notification_events()])
        self.retry_is_due(resumed)
        self.run_job(resumed)
        self.assert_keeper(resumed)

    def rearmed_then_stopped(self):
        """Absence is proven (intent re-armed, nothing sent), then the Job stops before sending."""
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        with patch.object(provenance, "mark_send_started", side_effect=RuntimeError("stopped before the send")):
            self.run_job(resumed)
        self.assertEqual(self.row(resumed)["status"], "failed")
        intent = self.intent()
        self.assertFalse(intent["send_started"])
        self.assertFalse(intent["settled"])
        self.assertEqual(self.provider.puts, 0)
        return resumed

    def assert_next_upload_is_not_blocked(self):
        with self.db() as connection:
            state = provenance.describe(connection, vault_id=2, path="report.txt")
        self.assertFalse(state["pending"], state)
        self.file.write_bytes(b"new content after the proven absence\n")
        job = self.queue_fresh()
        self.run_job(job)
        row = self.row(job)
        self.assertEqual(row["status"], "completed", row["message"])
        self.assertEqual(self.provider.puts, 1)
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) n FROM upload_intents WHERE NOT settled").fetchone()["n"], 0)

    def test_rearmed_intent_of_a_failed_job_does_not_block_the_next_upload(self):
        self.rearmed_then_stopped()
        self.assert_next_upload_is_not_blocked()

    def test_rearmed_intent_of_a_cancelled_job_does_not_block_the_next_upload(self):
        resumed = self.rearmed_then_stopped()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (resumed,))
        self.assert_next_upload_is_not_blocked()

    def test_an_unsent_intent_is_refreshed_when_the_cloud_inventory_changed_meanwhile(self):
        # Same Job: the intent was prepared but never sent; an external write landed since.
        self.stable_content()
        with patch.object(provenance, "mark_send_started", side_effect=psycopg.OperationalError("connection is bad")):
            job = self.queue()
            self.run_job(job)
        self.assertEqual(self.row(job)["status"], "retrying")
        stale = self.intent()
        self.assertFalse(stale["send_started"])
        self.provider.add(KEY, b"written by someone else", version="external-1")
        self.retry_is_due(job)
        self.run_job(job)
        # The send itself proceeds from a re-pinned intent. Cleanup of the pinned
        # namespace may still refuse the foreign write; that is a different, real block.
        self.assertEqual(self.provider.puts, 1)
        self.assertNotIn("identity_changed", self.row(job)["message"])
        with self.db() as connection:
            intents = connection.execute("SELECT * FROM upload_intents").fetchall()
        self.assertEqual(len(intents), 1)
        self.assertTrue(intents[0]["send_started"])
        self.assertIn("external-1", intents[0]["inventory_json"])
        self.assertNotEqual(intents[0]["token"], stale["token"])

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
