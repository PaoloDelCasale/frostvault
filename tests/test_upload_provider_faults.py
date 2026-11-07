"""#492: provider faults and refusals during upload reconciliation, without a database.

A refused S3 action is a per-file block only while a fresh bucket probe still passes
(otherwise it belongs to the readiness gate); throttling, request timeouts and
malformed provider answers are retried like 5xx; an unreadable Source Volume backs
off and is surfaced after a threshold. The database-backed flow is covered in
``test_upload_ambiguous_send_recovery``.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import (
    ClientError, EndpointConnectionError, HTTPClientError, IncompleteReadError, NoCredentialsError, ReadTimeoutError,
)
from botocore.parsers import ResponseParserError

from app import storage
from app.services import cloud_preflight_gate, upload_provenance as provenance
from app.services.upload_retry import classify_upload_failure, is_provider_transient


def client_error(code, status, operation="HeadObject"):
    return ClientError({"Error": {"Code": code, "Message": "provider text"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


def probe(ok):
    return patch.object(provenance, "_bucket_still_ready", return_value=ok)


class TransientProviderFaultTests(unittest.TestCase):
    def test_throttling_timeouts_and_non_http_answers_are_transient(self):
        transient = [
            client_error("SlowDown", 503, "ListObjectVersions"),
            client_error("SlowDown", 429, "ListObjectVersions"),
            client_error("TooManyRequests", 429),
            client_error("RequestTimeout", 400, "ListMultipartUploads"),
            client_error("RequestTimeout", 408),
            client_error("InternalError", 500),
            ResponseParserError("Unable to parse response (not well-formed)"),
            HTTPClientError(error="proxy sent html"),
            IncompleteReadError(actual_bytes=1, expected_bytes=9),
            ReadTimeoutError(endpoint_url="https://s3.invalid"),
            EndpointConnectionError(endpoint_url="https://s3.invalid"),
            ConnectionResetError("reset by peer"),
        ]
        for error in transient:
            with self.subTest(error=repr(error)):
                self.assertTrue(is_provider_transient(error))
                self.assertEqual(classify_upload_failure(error), "transient")

    def test_a_wrapped_transient_fault_is_still_transient(self):
        try:
            try:
                raise client_error("SlowDown", 429)
            except ClientError as inner:
                raise RuntimeError("listing failed") from inner
        except RuntimeError as outer:
            self.assertTrue(is_provider_transient(outer))

    def test_permission_credential_and_content_errors_are_not_transient(self):
        for error in (client_error("AccessDenied", 403), client_error("InvalidAccessKeyId", 403),
                      client_error("NoSuchKey", 404), client_error("InvalidArgument", 400),
                      NoCredentialsError(), RuntimeError("boom")):
            with self.subTest(error=repr(error)):
                self.assertFalse(is_provider_transient(error))
        self.assertEqual(classify_upload_failure(client_error("AccessDenied", 403)), "permanent")


class ListingFaultTests(unittest.TestCase):
    def listing(self, error):
        def call():
            raise error
        return lambda: provenance._guarded_listing(call, "bucket")

    def test_throttled_timed_out_and_malformed_listings_propagate_to_be_retried(self):
        for error in (client_error("SlowDown", 429, "ListObjectVersions"),
                      client_error("RequestTimeout", 400, "ListMultipartUploads"),
                      ResponseParserError("not xml"), HTTPClientError(error="closed")):
            with self.subTest(error=repr(error)), probe(True):
                with self.assertRaises(type(error)):
                    self.listing(error)()

    def test_an_unconfirmed_listing_is_still_a_changed_inventory(self):
        with probe(True), self.assertRaises(provenance.UploadBlocked) as caught:
            self.listing(client_error("InvalidArgument", 400, "ListObjectVersions"))()
        self.assertEqual(caught.exception.reason, "inventory_changed")

    def test_a_refused_listing_blocks_the_file_only_when_the_probe_passes(self):
        with probe(True), self.assertRaises(provenance.UploadBlocked) as caught:
            self.listing(client_error("AccessDenied", 403, "ListObjectVersions"))()
        self.assertEqual(caught.exception.reason, "list_permission_denied")
        self.assertFalse(cloud_preflight_gate.is_readiness_error(caught.exception))
        with probe(False), self.assertRaises(ClientError):
            self.listing(client_error("AccessDenied", 403, "ListObjectVersions"))()


class RefusedActionTests(unittest.TestCase):
    def test_each_refused_action_names_its_permission(self):
        cases = {"HeadObject": "read_permission_denied", "GetObject": "read_permission_denied",
                 "PutObject": "write_permission_denied", "CreateMultipartUpload": "write_permission_denied",
                 "UploadPart": "write_permission_denied", "CompleteMultipartUpload": "write_permission_denied",
                 "ListObjectVersions": "list_permission_denied", "DeleteObject": "cleanup_unavailable"}
        for operation, reason in cases.items():
            with self.subTest(operation=operation), probe(True):
                self.assertEqual(provenance.permission_denied_reason(client_error("AccessDenied", 403, operation), "bucket"), reason)

    def test_a_bodyless_head_403_is_a_refusal(self):
        with probe(True):
            self.assertEqual(provenance.permission_denied_reason(client_error("403", 403, "HeadObject"), "bucket"), "read_permission_denied")

    def test_a_failing_probe_or_a_credential_error_leaves_the_gate_in_charge(self):
        with probe(False):
            self.assertIsNone(provenance.permission_denied_reason(client_error("AccessDenied", 403, "PutObject"), "bucket"))
        with probe(True):
            for code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"):
                self.assertIsNone(provenance.permission_denied_reason(client_error(code, 403, "PutObject"), "bucket"))

    def test_an_unknown_operation_or_a_non_refusal_is_not_a_per_file_block(self):
        with probe(True):
            self.assertIsNone(provenance.permission_denied_reason(client_error("AccessDenied", 403, "GetBucketPolicy"), "bucket"))
            self.assertEqual(provenance.permission_denied_reason(client_error("AccessDenied", 403, "GetBucketPolicy"), "bucket", default="read_permission_denied"), "read_permission_denied")
            self.assertIsNone(provenance.permission_denied_reason(client_error("SlowDown", 429, "HeadObject"), "bucket"))

    def test_the_block_is_not_chained_to_the_provider_error(self):
        def denied():
            raise client_error("AccessDenied", 403, "HeadObject")
        with probe(True), self.assertRaises(provenance.UploadBlocked) as caught:
            provenance.guarded(denied, "bucket", default="read_permission_denied")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(caught.exception.reason, "read_permission_denied")

    def test_every_new_reason_has_a_remedy(self):
        for reason in ("read_permission_denied", "write_permission_denied", "list_permission_denied"):
            self.assertIn(reason, provenance.REMEDIES)
        self.assertIn("s3:PutObject", provenance.REMEDIES["write_permission_denied"])
        self.assertIn("s3:GetObjectVersion", provenance.REMEDIES["read_permission_denied"])


class WorkerRefusalTests(unittest.TestCase):
    job = {"id": 7, "action": "upload", "s3_bucket": "bucket", "vault_id": 2, "vault_file_id": "f", "path": "a.txt"}

    def run_refusal(self, error, *, ready=True, job=None):
        with probe(ready), patch.object(storage, "_record_upload_reconciliation_block") as record, \
                patch.object(storage, "set_job") as set_job:
            handled = storage._fail_upload_on_permission_refusal(job or self.job, error)
        return handled, record, set_job

    def test_a_refused_read_fails_the_job_and_leaves_a_durable_block(self):
        handled, record, set_job = self.run_refusal(client_error("AccessDenied", 403, "GetObject"))
        self.assertTrue(handled)
        record.assert_called_once_with(self.job, "read_permission_denied")
        self.assertEqual(set_job.call_args.args[:2], (7, "failed"))
        self.assertIn("read_permission_denied", set_job.call_args.args[2])

    def test_a_refused_write_fails_the_job_without_a_reconciliation_block(self):
        handled, record, set_job = self.run_refusal(client_error("AccessDenied", 403, "PutObject"))
        self.assertTrue(handled)
        record.assert_not_called()
        self.assertIn("s3:PutObject", set_job.call_args.args[2])

    def test_an_installation_wide_refusal_is_left_to_the_gate(self):
        handled, record, set_job = self.run_refusal(client_error("AccessDenied", 403, "PutObject"), ready=False)
        self.assertFalse(handled)
        record.assert_not_called()
        set_job.assert_not_called()

    def test_only_upload_jobs_are_handled_here(self):
        handled, _, _ = self.run_refusal(client_error("AccessDenied", 403, "PutObject"), job={**self.job, "action": "rename"})
        self.assertFalse(handled)

    def test_the_worker_handler_does_not_invalidate_the_gate_for_a_per_file_refusal(self):
        job = {**self.job, "claim_token": "t", "status": "queued", "source_root": "/s"}
        with patch.object(storage, "_runtime_settings"), patch.object(storage, "_load_claimed_job_status", return_value={"status": "queued"}), \
                patch.object(storage.source_layout, "vault_local_access", return_value=MagicMock(local_operations_allowed=True)), \
                patch.object(storage, "process_upload", side_effect=client_error("AccessDenied", 403, "HeadObject")), \
                patch.object(storage, "_claim_is_lost", return_value=False), \
                patch.object(storage, "_fail_upload_on_permission_refusal", return_value=True) as fail, \
                patch.object(cloud_preflight_gate, "invalidate") as invalidate:
            self.assertTrue(storage._process_claimed_job(job))
        fail.assert_called_once()
        invalidate.assert_not_called()


class SourceVolumeWaitTests(unittest.TestCase):
    def job(self, **changes):
        return {"id": 9, "action": "upload", "vault_id": 2, "vault_name": "Docs", "claim_token": "t",
                "retry_count": 3, "message_key": None, "message_params": None, **changes}

    def wait(self, job):
        with patch.object(storage, "schedule_upload_retry", return_value=True) as retry, \
                patch.object(storage, "set_job") as set_job, \
                patch.object(storage, "db"), patch.object(storage, "owned_connection"), \
                patch.object(storage.notification_service, "set_problem") as problem:
            storage._wait_for_source_volume(job)
        return retry, set_job, problem

    def test_the_first_wait_backs_off_without_spending_the_retry_budget(self):
        retry, set_job, problem = self.wait(self.job())
        set_job.assert_not_called()
        problem.assert_not_called()
        kwargs = retry.call_args.kwargs
        self.assertEqual(kwargs["retry_count"], 3)
        self.assertEqual(kwargs["remediation_suspension"], "source_volume_unavailable")
        self.assertEqual(kwargs["message_key"], "job.source_volume_waiting")
        self.assertEqual((kwargs["message_params"]["attempt"], kwargs["delay_seconds"]), (1, 2))

    def test_consecutive_waits_back_off_up_to_the_cap(self):
        since = datetime.now(timezone.utc).isoformat()
        delays = []
        for attempt in (1, 2, 3, 8, 20):
            job = self.job(message_key="job.source_volume_waiting",
                           message_params=f'{{"attempt": {attempt}, "since": "{since}"}}')
            retry, _, _ = self.wait(job)
            self.assertEqual(retry.call_args.kwargs["message_params"]["attempt"], attempt + 1)
            delays.append(retry.call_args.kwargs["delay_seconds"])
        self.assertEqual(delays, [4, 8, 16, 300, 300])

    def test_an_outage_older_than_the_threshold_opens_one_vault_problem(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=storage.SOURCE_VOLUME_NOTIFY_AFTER_SECONDS + 5)).isoformat()
        job = self.job(message_key="job.source_volume_waiting", message_params=f'{{"attempt": 4, "since": "{old}"}}')
        retry, _, problem = self.wait(job)
        problem.assert_called_once()
        kwargs = problem.call_args.kwargs
        self.assertEqual((kwargs["key"], kwargs["event"], kwargs["vault_id"], kwargs["active"]),
                         ("source_volume_unavailable:2", "source_volume_unavailable", 2, True))
        self.assertEqual(kwargs["params"]["reason"], "source_volume_unavailable")
        self.assertEqual(retry.call_args.kwargs["message_params"]["since"], old, "the outage start is kept")

    def test_a_short_outage_does_not_notify(self):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        _, _, problem = self.wait(self.job(message_key="job.source_volume_waiting",
                                           message_params=f'{{"attempt": 4, "since": "{recent}"}}'))
        problem.assert_not_called()

    def test_corrupt_wait_state_restarts_the_count(self):
        retry, _, _ = self.wait(self.job(message_key="job.source_volume_waiting", message_params='{"since": "nope"}'))
        self.assertEqual(retry.call_args.kwargs["message_params"]["attempt"], 1)

    def test_other_actions_keep_the_plain_queued_wait(self):
        retry, set_job, _ = self.wait(self.job(action="rename"))
        retry.assert_not_called()
        self.assertEqual(set_job.call_args.args[:2], (9, "queued"))
        self.assertEqual(set_job.call_args.kwargs["remediation_suspension"], "source_volume_unavailable")

    def test_an_unclaimed_job_is_left_alone(self):
        retry, set_job, problem = self.wait(self.job(claim_token=None))
        for mock in (retry, set_job, problem):
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
