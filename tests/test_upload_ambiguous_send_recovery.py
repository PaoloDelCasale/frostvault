"""#484/#486: recovery from interrupted sends through the real admission/claim/worker.

A send that never reached S3 (database or network lost around the PUT) must not
leave a file in ``ambiguous_send`` forever, and a transient condition must not be
reported as ``identity_changed``. TEST_POSTGRES_URL is mandatory (disposable PG16).
"""
import unittest
from types import SimpleNamespace
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
        self.enterContext(patch("app.storage._automatic_upload_state", return_value="ready"))

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
        self.assertEqual((row["status"], row["retry_count"], row["message_key"]), ("retrying", 0, "job.source_volume_waiting"), row["message"])
        self.assertEqual(self.notification_events(), [])
        self.assertEqual(self.provider.puts, 0)
        self.retry_is_due(job)
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
        self.assertEqual((row["status"], row["retry_count"]), ("retrying", 0), row["message"])
        self.assertTrue(self.intent()["send_started"])
        self.assertEqual(self.provider.puts, 0)
        self.assertEqual(self.notification_events(), [])
        self.retry_is_due(job)
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
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, patched, self.bucket_probe(True):
            self.run_job(resumed)
        invalidate.assert_not_called()
        return resumed

    def bucket_probe(self, ok):
        from app.services.s3_preflight import PreflightResult
        return patch("app.services.s3_preflight.check_bucket_readiness",
                     return_value=PreflightResult("bucket", "eu-west-1", ok, ()))

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

    def test_any_403_on_a_listing_blocks_the_file_when_the_bucket_is_still_ready(self):
        resumed = self.blocked_resume(patch.object(self.provider, "list_object_versions", side_effect=self.denied("Forbidden", 403)))
        self.assert_listing_denied_blocks_only_the_file(resumed)

    def test_revoked_permissions_suspend_through_the_gate_instead_of_failing_the_job(self):
        # Fresh probe fails: the refusal is installation-wide, so the Job stays
        # queued behind the readiness gate and does not fail in mass.
        from app.services import cloud_preflight_gate
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.http_client().post("/api/upload/retry-failed?vault_id=2")
        resumed = max(j["id"] for j in self.jobs())
        for code in ("AccessDenied", "Forbidden"):
            with self.subTest(code=code), patch.object(cloud_preflight_gate, "invalidate") as invalidate, \
                    patch.object(self.provider, "list_object_versions", side_effect=self.denied(code, 403)), self.bucket_probe(False):
                self.run_job(resumed)
                invalidate.assert_called_once()
                self.assertNotEqual(self.row(resumed)["status"], "failed", self.row(resumed)["message"])
                with self.db() as connection:
                    self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())

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

    def resume_ambiguous(self, *, foreign_version=False):
        """An ambiguous send is resumed by Retry failed uploads; returns the resumed Job."""
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        if foreign_version:
            # Something is listed for the key, so reconciliation must HEAD it.
            self.provider.add(KEY, b"another writer", {})
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        return max(j["id"] for j in self.jobs())

    def test_403_on_the_version_head_blocks_the_file_when_the_bucket_is_still_ready(self):
        # #492 F4: the readiness probe never exercises HeadObject, so sending this
        # refusal to the gate would reopen it at once and re-claim the Job forever.
        from app.services import cloud_preflight_gate
        resumed = self.resume_ambiguous(foreign_version=True)
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(True), \
                patch.object(self.provider, "head_object", side_effect=self.denied(operation="HeadObject")):
            self.run_job(resumed)
        invalidate.assert_not_called()
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed", row["message"])
        self.assertIn("read_permission_denied", row["message"])
        self.assertIn("s3:GetObjectVersion", row["message"])
        self.assertNotIn("S3 readiness", row["message"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        with self.db() as connection:
            self.assertEqual(connection.execute("SELECT reason FROM snapshot_reconciliation_blocks").fetchone()["reason"], "read_permission_denied")
            self.assertIsNone(connection.execute("SELECT 1 FROM notifications WHERE event='cloud_preflight_blocked'").fetchone())

    def test_403_on_the_version_head_goes_to_the_gate_when_the_probe_fails(self):
        from app.services import cloud_preflight_gate
        resumed = self.resume_ambiguous(foreign_version=True)
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(False), \
                patch.object(self.provider, "head_object", side_effect=self.denied(operation="HeadObject")):
            self.run_job(resumed)
        invalidate.assert_called_once()
        self.assertNotEqual(self.row(resumed)["status"], "failed", self.row(resumed)["message"])
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())

    def test_403_on_the_put_fails_only_that_file_with_the_missing_permission(self):
        from app.services import cloud_preflight_gate
        self.stable_content()
        refusal = self.denied(operation="PutObject")
        refusal.upload_no_write_operation = "PutObject"
        def refuse(stage, **kwargs):
            if stage == "before_put":
                raise refusal
        self.provider.hook = refuse
        job = self.queue()
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(True):
            self.run_job(job)
        invalidate.assert_not_called()
        row = self.row(job)
        self.assertEqual(row["status"], "failed", row["message"])
        self.assertIn("write_permission_denied", row["message"])
        self.assertIn("s3:PutObject", row["message"])
        self.assertEqual(self.provider.puts, 0)
        # Nothing was stored: the refusal is journaled, no durable reconciliation block remains.
        self.assertFalse(self.intent()["send_started"])
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM notifications WHERE event='cloud_preflight_blocked'").fetchone())
        self.provider.hook = lambda *args, **kwargs: None
        retried = self.queue_fresh()
        self.run_job(retried)
        self.assertEqual(self.row(retried)["status"], "completed", self.row(retried)["message"])
        self.assertEqual(self.provider.puts, 1)

    def test_403_on_the_put_goes_to_the_gate_when_the_probe_fails(self):
        from app.services import cloud_preflight_gate
        self.stable_content()
        refusal = self.denied(operation="PutObject")
        refusal.upload_no_write_operation = "PutObject"
        def refuse(stage, **kwargs):
            if stage == "before_put":
                raise refusal
        self.provider.hook = refuse
        job = self.queue()
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(False):
            self.run_job(job)
        invalidate.assert_called_once()
        self.assertNotEqual(self.row(job)["status"], "failed", self.row(job)["message"])

    def test_throttling_timeouts_and_non_http_answers_on_a_listing_are_retried(self):
        # #492 F7: transient provider answers are retried with backoff, never a changed inventory.
        from botocore.exceptions import HTTPClientError
        from botocore.parsers import ResponseParserError
        resumed = self.resume_ambiguous()
        errors = [self.denied("SlowDown", 429), self.denied("RequestTimeout", 400, "ListMultipartUploads"),
                  ResponseParserError("Unable to parse response"), HTTPClientError(error="connection closed")]
        for error in errors:
            with self.subTest(error=repr(error)):
                with patch.object(self.provider, "list_object_versions", side_effect=error):
                    self.run_job(resumed)
                row = self.row(resumed)
                self.assertEqual(row["status"], "retrying", row["message"])
                with self.db() as connection:
                    self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
                self.assertNotIn("upload_reconciliation", [n["event"] for n in self.notification_events()])
                self.retry_is_due(resumed)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.provider.puts, 1)

    def test_an_unreadable_root_backs_off_then_notifies_and_recovers_on_its_own(self):
        # #492: a persistently unreadable root waits with backoff, then becomes visible.
        import json
        from datetime import datetime, timedelta, timezone
        self.stable_content()
        job = self.queue()
        unreadable = patch("app.storage.vault_relocation.root_identity", side_effect=VaultRelocationError("inaccessible", "volume not mounted"))
        with unreadable:
            self.run_job(job)
            row = self.row(job)
            self.assertEqual((row["status"], row["retry_count"], row["message_key"]), ("retrying", 0, "job.source_volume_waiting"))
            first = json.loads(row["message_params"])
            self.assertEqual(first["attempt"], 1)
            self.assertGreater(datetime.fromisoformat(row["retry_after"]), datetime.now(timezone.utc))
            self.assertNotIn("source_volume_unavailable", [n["event"] for n in self.notification_events()])
            self.retry_is_due(job)
            self.run_job(job)
            second = json.loads(self.row(job)["message_params"])
            self.assertEqual((second["attempt"], second["since"]), (2, first["since"]))
            # The outage is now older than the threshold.
            old = (datetime.now(timezone.utc) - timedelta(seconds=storage.SOURCE_VOLUME_NOTIFY_AFTER_SECONDS + 60)).isoformat()
            with self.db() as connection:
                connection.execute("UPDATE jobs SET message_params=%s WHERE id=%s", (json.dumps({**second, "since": old}), job))
            self.retry_is_due(job)
            self.run_job(job)
        opened = [n for n in self.notification_events() if n["event"] == "source_volume_unavailable"]
        self.assertEqual([n["problem_status"] for n in opened], ["open"])
        self.assertEqual(self.row(job)["retry_count"], 0)
        self.retry_is_due(job)
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual([n["problem_status"] for n in self.notification_events() if n["event"] == "source_volume_unavailable"], ["resolved"])

    def open_source_volume_problem(self):
        """A waiting upload whose outage is older than the threshold: the Vault problem is open."""
        import json
        from datetime import datetime, timedelta, timezone
        self.stable_content()
        job = self.queue()
        with patch("app.storage.vault_relocation.root_identity", side_effect=VaultRelocationError("inaccessible", "volume not mounted")):
            self.run_job(job)
            params = json.loads(self.row(job)["message_params"])
            old = (datetime.now(timezone.utc) - timedelta(seconds=storage.SOURCE_VOLUME_NOTIFY_AFTER_SECONDS + 60)).isoformat()
            with self.db() as connection:
                connection.execute("UPDATE jobs SET message_params=%s WHERE id=%s", (json.dumps({**params, "since": old}), job))
            self.retry_is_due(job)
            self.run_job(job)
        self.assertEqual([n["problem_status"] for n in self.notification_events() if n["event"] == "source_volume_unavailable"], ["open"])
        return job

    def source_volume_problem_status(self):
        return [n["problem_status"] for n in self.notification_events() if n["event"] == "source_volume_unavailable"]

    def test_the_source_volume_problem_ends_when_its_waiting_job_is_cancelled(self):
        job = self.open_source_volume_problem()
        with self.unreadable_root():
            self.assertEqual(storage.sweep_source_volume_problems(), 0, "an upload still waits for the volume")
        self.assertEqual(self.source_volume_problem_status(), ["open"])
        storage.cancel_jobs([job])
        self.assertEqual(storage.sweep_source_volume_problems(), 1)
        self.assertEqual(self.source_volume_problem_status(), ["resolved"])
        self.assertEqual(storage.sweep_source_volume_problems(), 0)

    def unreadable_root(self):
        """A healthy mount whose enrolled root cannot be observed (ENOENT/EIO/ESTALE)."""
        return patch("app.storage.vault_relocation.root_identity", side_effect=VaultRelocationError("inaccessible", "stale file handle"))

    def test_the_sweep_does_not_flap_the_problem_while_the_root_stays_unobservable(self):
        # vault_local_access passes, but the upload keeps waiting because the root identity
        # cannot be read: the sweep must apply the same criterion or the notice would be
        # resolved and re-opened (read_at cleared) every cycle.
        job = self.open_source_volume_problem()
        with self.unreadable_root():
            for _ in range(3):
                self.assertEqual(storage.sweep_source_volume_problems(), 0)
            self.assertEqual(self.source_volume_problem_status(), ["open"])
            self.retry_is_due(job)
            self.run_job(job)
        self.assertEqual(self.source_volume_problem_status(), ["open"], "one notice, never re-opened")
        self.assertEqual(self.row(job)["message_key"], "job.source_volume_waiting")

    def test_the_source_volume_problem_ends_when_the_root_is_readable_without_other_uploads(self):
        self.open_source_volume_problem()
        with patch("app.storage.source_layout.vault_local_access",
                   return_value=SimpleNamespace(local_operations_allowed=False, volume_health="missing")):
            self.assertEqual(storage.sweep_source_volume_problems(), 0)
        self.assertEqual(self.source_volume_problem_status(), ["open"])
        self.assertEqual(storage.sweep_source_volume_problems(), 1)
        self.assertEqual(self.source_volume_problem_status(), ["resolved"])

    def test_a_cold_storage_403_on_the_historical_get_stays_proof_unavailable_and_never_reaches_the_gate(self):
        # S3 answers 403 InvalidObjectState to the GET of an archived (Glacier) version.
        from botocore.exceptions import ClientError
        from app.services import cloud_preflight_gate
        self.provider.add(KEY, fixtures.A, version="cold-orphan")
        def archived(stage, **kwargs):
            if stage == "get":
                raise ClientError({"Error": {"Code": "InvalidObjectState", "Message": "provider text"},
                                   "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")
        self.provider.hook = archived
        job = self.queue()
        with patch.object(cloud_preflight_gate, "invalidate") as invalidate, self.bucket_probe(True):
            self.run_job(job)
        invalidate.assert_not_called()
        row = self.row(job)
        self.assertEqual(row["status"], "failed", row["message"])
        self.assertEqual((self.provider.deletes, len(self.provider.objects)), (0, 2))
        with self.db() as connection:
            state = provenance.describe(connection, vault_id=2, path="report.txt")
        self.assertEqual(state["reason"], "proof_unavailable")
        self.assertIn("explicit restore", state["remedy"])

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
        resumed = self.rearmed_then_stopped()
        original = min(j["id"] for j in self.jobs())
        episode = self.episode()
        self.assertEqual(episode["state"], "blocked")
        self.assert_next_upload_is_not_blocked()
        # The unlinked ordinary upload protected the file: the void episode must not
        # stay open with the original and remediation failures still reported.
        completed = max(j["id"] for j in self.jobs())
        with self.db() as connection:
            row = connection.execute("SELECT * FROM upload_remediations").fetchone()
            self.assertEqual((row["state"], row["phase"], row["reason"]), ("resolved", "completed", None))
            self.assertEqual(row["resolved_by_job_id"], completed)
            self.assertIsNotNone(row["cleanup_inventory_job_id"])
            notifications.reconcile_problem_states(connection)
            for job in (original, resumed):
                failed = connection.execute("SELECT problem_status FROM notifications WHERE event='job_failed' AND job_id=%s", (job,)).fetchone()
                self.assertEqual(failed["problem_status"], "resolved", job)
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 0})

    def test_a_void_episode_never_offers_resume_and_never_occupies_a_retry_slot(self):
        self.rearmed_then_stopped()
        self.assertEqual(self.episode()["state"], "blocked")
        with self.db() as connection:
            connection.execute("INSERT INTO snapshot_reconciliation_blocks(vault_file_id,reason,updated_at) SELECT vault_file_id,'identity_changed','2026-01-01T00:00:00+00:00' FROM upload_remediations")
            state = provenance.describe(connection, vault_id=2, path="report.txt")
            self.assertTrue(state["pending"])
            self.assertFalse(state["can_resume"], state)
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            with patch.object(remediation, "TARGET_BATCH", 1), patch.object(remediation, "admit") as admit:
                self.assertEqual(remediation.retry_vault(connection, vault, 1, "2026-01-02T00:00:00+00:00"), 0)
                admit.assert_not_called()
            sweep_rows = connection.execute(f"SELECT count(*) n FROM upload_remediations r WHERE NOT {remediation.VOID_SQL}").fetchone()["n"]
            self.assertEqual(sweep_rows, 0)

    def test_rearmed_intent_of_a_cancelled_job_does_not_block_the_next_upload(self):
        resumed = self.rearmed_then_stopped()
        with self.db() as connection:
            connection.execute("UPDATE jobs SET status='cancelled' WHERE id=%s", (resumed,))
        self.assert_next_upload_is_not_blocked()

    def test_an_unsent_intent_is_refreshed_when_the_cloud_inventory_changed_meanwhile(self):
        # Same Job: the intent was prepared but never sent; an external write landed since.
        self.stable_content()
        job = self.queue()
        fixtures.prepare_unsent(self, job)
        with patch.object(provenance, "mark_send_started", side_effect=psycopg.OperationalError("connection is bad")):
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

    def remediation_job(self):
        self.stable_content()
        original = self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        return original, max(j["id"] for j in self.jobs())

    def episode(self):
        with self.db() as connection:
            return connection.execute("SELECT state,reason,current_job_id FROM upload_remediations").fetchone()

    def test_a_remediation_job_resumed_after_a_restart_waits_for_the_source_volume(self):
        # #486: right after docker kill the root cannot be read for a while. The
        # remediation attempt must wait in verification, never fail identity_changed.
        _, resumed = self.remediation_job()
        before = [n["event"] for n in self.notification_events()]
        with patch("app.storage.vault_relocation.root_identity", side_effect=VaultRelocationError("inaccessible", "volume not mounted yet")):
            self.run_job(resumed)
        row = self.row(resumed)
        self.assertEqual(row["status"], "retrying", row["message"])
        self.assertNotIn("identity_changed", row["message"])
        self.assertEqual(self.episode()["current_job_id"], resumed)
        self.assertEqual((self.episode()["state"], self.episode()["reason"]), ("suspended", "source_volume_unavailable"))
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
        self.assertEqual([n["event"] for n in self.notification_events()], before)
        self.retry_is_due(resumed)
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.episode()["state"], "resolved")

    def test_a_job_of_a_suspended_vault_waits_and_restarts_when_the_vault_is_reactivated(self):
        _, resumed = self.remediation_job()
        with self.db() as connection:
            connection.execute("UPDATE vaults SET enabled=FALSE WHERE id=2")
        self.run_job(resumed)
        row = self.row(resumed)
        self.assertEqual(row["status"], "queued", row["message"])
        self.assertEqual(self.episode()["reason"], "vault_suspended")
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
        self.assertNotIn("upload_reconciliation", [n["event"] for n in self.notification_events()])
        with self.db() as connection:
            connection.execute("UPDATE vaults SET enabled=TRUE WHERE id=2")
        self.run_job(resumed)
        self.assert_keeper(resumed)
        self.assertEqual(self.episode()["state"], "resolved")

    def test_source_volume_loss_during_cleanup_requeues_without_a_cleanup_block(self):
        job = self.queue()
        with patch("app.services.snapshot_cleanup.cleanup", side_effect=storage.SourceVolumeUnavailable("volume gone")):
            self.run_job(job)
        row = self.row(job)
        self.assertNotEqual(row["status"], "failed", row["message"])
        with self.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
        self.assertNotIn("upload_reconciliation", [n["event"] for n in self.notification_events()])

    def set_auto_upload(self, enabled, **changes):
        from dataclasses import replace
        from app.services import operation_policies
        with self.db() as connection:
            policy = operation_policies.get_policy(connection, 2)
            operation_policies.set_policy(connection, 2, replace(policy, auto_upload=enabled, **changes))

    def test_a_manual_upload_is_not_stopped_by_paused_protection_after_a_network_retry(self):
        # Paused automatic protection gates only reconcile-only attempts; a user's
        # explicit upload that lost its database is reconciled and sent.
        job = self.fail_before_provider_request(psycopg.OperationalError("connection is bad: Network is unreachable"))
        self.assertEqual(self.row(job)["status"], "retrying", self.row(job)["message"])
        self.assertEqual(self.row(job)["origin"], "manual")
        self.set_auto_upload(False)
        self.retry_is_due(job)
        self.run_job(job)
        self.assert_keeper(job)
        self.assertEqual(self.provider.puts, 1)

    def test_a_proven_absence_never_starts_an_upload_while_protection_is_paused(self):
        self.stable_content()
        self.fail_before_provider_request(RuntimeError("worker crashed before the request"))
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        resumed = max(j["id"] for j in self.jobs())
        self.assertTrue(self.row(resumed)["upload_reconcile_only"])
        # Real policy: protection is paused after the attempt was claimed, while
        # the absence proof reads the bucket.
        paused = []
        def pause(stage, **kwargs):
            if stage == "list" and not paused:
                paused.append(True)
                self.set_auto_upload(False)
        self.provider.hook = pause
        self.run_job(resumed)
        self.provider.hook = lambda *args, **kwargs: None
        row = self.row(resumed)
        self.assertEqual(row["status"], "failed")
        self.assertIn("protection_paused", row["message"])
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))
        intent = self.intent()
        self.assertTrue(intent["send_started"])
        self.assertEqual(intent["blocked_reason"], "protection_paused")
        # Once protection is resumed the same explicit reconcile proves absence and sends.
        self.set_auto_upload(True)
        self.assertEqual(self.http_client().post("/api/upload/retry-failed?vault_id=2").json(), {"item_count": 1})
        again = max(j["id"] for j in self.jobs())
        self.run_job(again)
        self.assert_keeper(again)
        self.assertEqual(self.provider.puts, 1)

    def job_with_vault(self, job):
        with self.db() as connection:
            return connection.execute("SELECT j.*,v.source_root,v.s3_bucket,v.s3_prefix FROM jobs j JOIN vaults v ON v.id=j.vault_id WHERE j.id=%s", (job,)).fetchone()

    def test_a_job_that_is_already_over_names_its_own_state_and_leaves_no_block(self):
        for status, reason in (("cancelled", "owner_cancelled"), ("failed", "already_resolved"), ("completed", "already_resolved")):
            with self.subTest(status=status):
                job = self.queue_fresh()
                with self.db() as connection:
                    connection.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job))
                current = self.job_with_vault(job)
                with self.db() as connection, self.assertRaises(provenance.UploadBlocked) as raised:
                    provenance.target(connection, current, key="docs/report.txt", provider="p")
                self.assertEqual(raised.exception.reason, reason)
                storage._record_upload_reconciliation_block(current, reason)
                with self.db() as connection:
                    self.assertIsNone(connection.execute("SELECT 1 FROM snapshot_reconciliation_blocks").fetchone())
                    self.assertIsNone(connection.execute("SELECT 1 FROM notifications WHERE event='upload_reconciliation'").fetchone())

    def test_resuming_an_episode_that_is_already_resolved_says_already_resolved(self):
        _, resumed = self.remediation_job()
        self.run_job(resumed)
        self.assert_keeper(resumed)
        with self.db() as connection:
            episode = connection.execute("SELECT id FROM upload_remediations").fetchone()["id"]
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            for remediation_id, reason in ((episode, "already_resolved"), ("no-such-episode", "proof_unavailable")):
                with self.assertRaises(provenance.UploadBlocked) as raised:
                    remediation.schedule_resume(connection, vault=vault, path="report.txt", user_id=1,
                                                timestamp="2026-01-01T00:00:00+00:00", remediation_id=remediation_id)
                self.assertEqual(raised.exception.reason, reason)

    def test_a_superseded_keeper_is_reported_as_target_superseded_not_identity_changed(self):
        from app.services import snapshot_cleanup
        job = self.queue()
        self.run_job(job)
        keeper = self.assert_keeper(job)
        with self.db() as connection:
            newer = dict(connection.execute("SELECT * FROM archive_versions WHERE id=%s", (keeper["id"],)).fetchone())
            newer.update(id="newer-version", version_number=keeper["version_number"] + 1, provider_version_id="newer-provider-version")
            columns = list(newer)
            connection.execute(f"INSERT INTO archive_versions({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})", [newer[c] for c in columns])
            with self.assertRaises(provenance.UploadBlocked) as raised:
                snapshot_cleanup._keeper(connection, self.job_with_vault(job), keeper["id"])
        self.assertEqual(raised.exception.reason, "target_superseded")

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
