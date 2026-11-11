"""Wire-level managed upload guarantees; no live S3, full suite or builds."""
from __future__ import annotations

import base64
import io
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from botocore.config import Config
from botocore.stub import ANY, Stubber

from app.services.s3_upload import upload_file, _shared_budget
from app.storage import OperationCancelled, _stream_plaintext_for_verification


class ManagedUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "file"
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test",
                                   aws_secret_access_key="test", config=Config(retries={"mode": "standard", "total_max_attempts": 1}))

    def upload(self, payload=b"content", **kwargs):
        self.path.write_bytes(payload)
        return upload_file(self.client, source=self.path, bucket="bucket", key="vaults/id/file",
                           check_active=kwargs.pop("check_active", lambda: None),
                           on_progress=lambda _: None, **kwargs)

    def test_put_uses_server_validated_checksum_and_response_version_without_head(self):
        checksum = base64.b64encode(zlib.crc32(b"content").to_bytes(4, "big")).decode()
        with Stubber(self.client) as stub:
            stub.add_response("put_object", {"VersionId": "response-not-current", "ChecksumCRC32": checksum},
                              {"Bucket": "bucket", "Key": "vaults/id/file", "Body": ANY,
                               "ChecksumAlgorithm": "CRC32", "ChecksumCRC32": checksum})
            self.assertEqual(self.upload()["VersionId"], "response-not-current")
            stub.assert_no_pending_responses()

    def test_manager_completion_notifies_waiter_without_polling_the_sdk_future(self):
        checksum = base64.b64encode(zlib.crc32(b"content").to_bytes(4, "big")).decode()
        with Stubber(self.client) as stub, patch(
            "s3transfer.futures.TransferFuture.done",
            side_effect=AssertionError("completion must notify, not poll the SDK future"),
        ):
            stub.add_response("put_object", {"VersionId": "notified", "ChecksumCRC32": checksum})
            self.assertEqual(self.upload()["VersionId"], "notified")
            stub.assert_no_pending_responses()

    def test_missing_version_or_checksum_fails_closed(self):
        for response in ({"VersionId": "v"}, {"ChecksumCRC32": "abc"}, {"VersionId": "null"}):
            with self.subTest(response=response), Stubber(self.client) as stub:
                stub.add_response("put_object", response)
                if response == {"VersionId": "v"}:
                    stub.add_response("head_object", {"VersionId": "v"})
                with self.assertRaises(RuntimeError):
                    self.upload()

    def test_missing_completion_checksum_requires_exact_version_full_object_proof(self):
        checksum = base64.b64encode(zlib.crc32(b"content").to_bytes(4, "big")).decode()
        for head, succeeds in (({"VersionId": "response-v", "ChecksumType": "FULL_OBJECT", "ChecksumCRC32": checksum}, True),
                               ({"VersionId": "current-other", "ChecksumType": "FULL_OBJECT", "ChecksumCRC32": checksum}, False),
                               ({"VersionId": "response-v", "ChecksumType": "COMPOSITE", "ChecksumCRC32": checksum}, False),
                               ({"VersionId": "response-v", "ChecksumType": "FULL_OBJECT", "ChecksumCRC32": "wrong"}, False)):
            with self.subTest(head=head), Stubber(self.client) as stub:
                stub.add_response("put_object", {"VersionId": "response-v"})
                stub.add_response("head_object", head, {"Bucket": "bucket", "Key": "vaults/id/file", "VersionId": "response-v", "ChecksumMode": "ENABLED"})
                if succeeds:
                    self.assertEqual(self.upload()["VersionId"], "response-v")
                else:
                    with self.assertRaises(RuntimeError):
                        self.upload()
                stub.assert_no_pending_responses()

    def test_multipart_full_object_checksum_and_response_identity(self):
        payload = b"x" * (9 * 1024 * 1024)
        checksum = base64.b64encode(zlib.crc32(payload).to_bytes(4, "big")).decode()
        with Stubber(self.client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"},
                              {"Bucket": "bucket", "Key": "vaults/id/file", "ChecksumAlgorithm": "CRC32", "ChecksumType": "FULL_OBJECT"})
            for i in range(2):
                stub.add_response("upload_part", {"ETag": f"part-{i}", "ChecksumCRC32": "part-crc"})
            stub.add_response("complete_multipart_upload", {"VersionId": "completed-response", "ChecksumCRC32": checksum, "ChecksumType": "FULL_OBJECT"},
                              {"Bucket": "bucket", "Key": "vaults/id/file", "UploadId": "mpu", "MultipartUpload": ANY,
                               "ChecksumType": "FULL_OBJECT", "ChecksumCRC32": checksum})
            self.assertEqual(self.upload(payload)["VersionId"], "completed-response")
            stub.assert_no_pending_responses()

    def test_part_failure_explicitly_aborts_and_does_not_complete_or_delete(self):
        with Stubber(self.client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            stub.add_client_error("upload_part", service_error_code="BadDigest")
            # There can be a second in-flight part; serialize requests for deterministic failure.
            stub.add_response("abort_multipart_upload", {}, {"Bucket": "bucket", "Key": "vaults/id/file", "UploadId": "mpu"})
            from boto3.s3.transfer import TransferConfig
            with patch("app.services.s3_upload.TransferConfig", return_value=TransferConfig(max_concurrency=1)):
                with self.assertRaises(Exception):
                    self.upload(b"x" * (9 * 1024 * 1024))
            stub.assert_no_pending_responses()

    def test_cancel_during_part_waits_then_aborts(self):
        entered = threading.Event()
        release = threading.Event()
        real_part = self.client.upload_part

        def part(**kwargs):
            entered.set()
            release.wait(5)
            return real_part(**kwargs)

        def active():
            if entered.is_set():
                release.set()
                raise OperationCancelled("Upload stopped")

        from boto3.s3.transfer import TransferConfig
        with Stubber(self.client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            stub.add_response("upload_part", {"ETag": "part"})
            stub.add_response("abort_multipart_upload", {}, {"Bucket": "bucket", "Key": "vaults/id/file", "UploadId": "mpu"})
            with patch.object(self.client, "upload_part", side_effect=part), patch("app.services.s3_upload.TransferConfig", return_value=TransferConfig(max_concurrency=1)):
                with self.assertRaises(OperationCancelled):
                    self.upload(b"x" * (9 * 1024 * 1024), check_active=active)
            stub.assert_no_pending_responses()

    def test_interrupted_completion_preserves_original_error_and_never_deletes(self):
        from botocore.exceptions import ClientError
        with Stubber(self.client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            for i in range(2):
                stub.add_response("upload_part", {"ETag": f"part-{i}"})
            stub.add_client_error("complete_multipart_upload", service_error_code="RequestTimeout")
            # The object may already be committed. Abort is safe/idempotent;
            # deleting a completed version to roll back would not be safe.
            stub.add_client_error("abort_multipart_upload", service_error_code="NoSuchUpload")
            with self.assertRaises(ClientError) as error:
                self.upload(b"x" * (9 * 1024 * 1024))
            self.assertEqual(error.exception.response["Error"]["Code"], "RequestTimeout")
            stub.assert_no_pending_responses()

    def test_source_change_during_checksum_pass_prevents_transfer(self):
        checks = 0
        def mutate():
            nonlocal checks
            checks += 1
            if checks == 2:
                self.path.write_bytes(b"new-longer-content")
        with patch.object(self.client, "put_object") as put:
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                self.upload(check_active=mutate)
            put.assert_not_called()

    def test_bandwidth_budget_is_shared_across_concurrent_managers(self):
        self.assertIs(_shared_budget(1024), _shared_budget(1024))
        with patch("app.services.s3_upload.TransferManager") as factory:
            factory.return_value.upload.side_effect = RuntimeError("network interrupted")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.upload(max_bandwidth=1024)
            self.assertIs(factory.return_value._bandwidth_limiter, _shared_budget(1024))
            self.assertEqual(factory.call_args.args[1].max_bandwidth, 1024)


class ExactVersionReadTests(unittest.TestCase):
    def test_only_exact_version_is_read_and_body_is_closed(self):
        import hashlib
        body = io.BytesIO(b"exact")
        client = Mock()
        client.get_object.return_value = {"Body": body, "VersionId": "upload-response"}
        with patch("app.storage.s3_client", return_value=client), patch("app.storage.ensure_job_active"), patch("app.storage.set_job_progress"):
            digest, size = _stream_plaintext_for_verification({"id": 1, "s3_bucket": "bucket"}, target={"object_key": "key", "provider_version_id": "upload-response"}, source_size=5)
        self.assertEqual((digest, size), (hashlib.sha256(b"exact").hexdigest(), 5))
        self.assertTrue(body.closed)
        client.get_object.assert_called_once_with(Bucket="bucket", Key="key", VersionId="upload-response")

    def test_wrong_version_and_interruption_never_return_trust(self):
        for version in ("wrong", "expected"):
            body = io.BytesIO(b"bytes")
            client = Mock()
            client.get_object.return_value = {"Body": body, "VersionId": version}
            active = Mock(side_effect=[None, OperationCancelled("stopped")])
            with patch("app.storage.s3_client", return_value=client), patch("app.storage.ensure_job_active", active):
                with self.assertRaises(RuntimeError):
                    _stream_plaintext_for_verification({"id": 1, "s3_bucket": "bucket"}, target={"object_key": "key", "provider_version_id": "expected"}, source_size=5)
            self.assertTrue(body.closed)
