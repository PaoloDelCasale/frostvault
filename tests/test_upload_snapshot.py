"""No DB/network: independent snapshots, pinned SHA256 multipart wire seam."""
import base64
import hashlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber, ANY
from s3transfer.futures import TransferCoordinator
from s3transfer.upload import InterruptReader
from s3transfer.utils import ReadFileChunk

from app.services.s3_upload import upload_file, _UploadClient, _shared_budget
from app.services.upload_snapshot import (
    CHUNK_SIZE, MULTIPART_THRESHOLD, prepare_upload_snapshot, UploadSnapshot,
    composite_checksum, sha256_b64, check_s3_proof, UploadProofMismatch,
    unsupported_checksum_api, validated_s3_proof,
)
from app.storage import OperationCancelled


class UploadSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "content"
        self.path.write_bytes(b"snapshot A")

    def snapshot(self, payload=None):
        if payload is not None:
            self.path.write_bytes(payload)
        return prepare_upload_snapshot(self.path, lambda: None)[0]

    def test_plaintext_and_single_put_proof_from_one_snapshot(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.plaintext_sha256, hashlib.sha256(b"snapshot A").hexdigest())
        self.assertEqual(snapshot.checksum_sha256, sha256_b64(b"snapshot A"))
        self.assertEqual(snapshot.checksum_type, "FULL_OBJECT")
        self.assertEqual(UploadSnapshot.from_json(snapshot.as_json()), snapshot)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_multipart_composite_uses_binary_part_hashes_and_correct_boundaries(self):
        payload = b"a" * MULTIPART_THRESHOLD + b"last"
        snapshot = self.snapshot(payload)
        parts = (sha256_b64(payload[:MULTIPART_THRESHOLD]), sha256_b64(payload[MULTIPART_THRESHOLD:]))
        self.assertEqual(snapshot.part_checksums, parts)
        self.assertEqual(snapshot.checksum_sha256, composite_checksum(parts))
        self.assertNotEqual(snapshot.checksum_sha256.split("-")[0], sha256_b64(payload))
        self.assertEqual(snapshot.part_length(2), 4)
        self.assertEqual(UploadSnapshot.from_json(snapshot.as_json()), snapshot)
        # One-part multipart at the threshold is still COMPOSITE, never plaintext.
        single_part = self.snapshot(b"a" * MULTIPART_THRESHOLD)
        self.assertTrue(single_part.checksum_sha256.endswith("-1"))
        self.assertEqual(single_part.checksum_type, "COMPOSITE")

    def test_empty_file_has_whole_object_sha256(self):
        snapshot = self.snapshot(b"")
        self.assertEqual(snapshot.checksum_sha256, sha256_b64(b""))
        self.assertEqual(UploadSnapshot.from_json(snapshot.as_json()), snapshot)

    def test_mutation_or_path_replacement_during_hash_never_produces_evidence(self):
        for replace in (False, True):
            self.path.write_bytes(b"original")
            calls = 0
            def active():
                nonlocal calls
                calls += 1
                if calls == 2:
                    if replace:
                        other = self.path.with_name("replacement")
                        other.write_bytes(b"replaced")
                        other.replace(self.path)
                    else:
                        self.path.write_bytes(b"changed-and-longer")
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                prepare_upload_snapshot(self.path, active)

    def test_cancellation_interrupts_hash(self):
        with self.assertRaises(OperationCancelled):
            prepare_upload_snapshot(self.path, Mock(side_effect=OperationCancelled("stop")))

    def test_proof_absence_is_distinct_from_wrong_checksum_type_identity_and_size(self):
        snapshot = self.snapshot()
        self.assertFalse(check_s3_proof({"VersionId": "v"}, version_id="v", snapshot=snapshot))
        valid = {"VersionId": "v", "ChecksumType": "FULL_OBJECT", "ChecksumSHA256": snapshot.checksum_sha256}
        self.assertTrue(check_s3_proof(valid, version_id="v", snapshot=snapshot))
        for changes in ({"ChecksumSHA256": "wrong"}, {"ChecksumType": "COMPOSITE"}, {"VersionId": "other"}, {"ContentLength": 99}):
            with self.subTest(changes=changes), self.assertRaises(UploadProofMismatch):
                check_s3_proof({**valid, **changes}, version_id="v", snapshot=snapshot)
        self.assertTrue(check_s3_proof({"VersionId": "v", "ChecksumSHA256": snapshot.checksum_sha256}, version_id="v", snapshot=snapshot, completion=True))
        self.assertFalse(check_s3_proof({"VersionId": "v", "ChecksumSHA256": snapshot.checksum_sha256}, version_id="v", snapshot=snapshot))

    def test_observed_proof_is_normalized_only_after_independent_validation(self):
        snapshot = self.snapshot()
        proof = {"VersionId": "exact", "ChecksumType": "FULL_OBJECT",
                 "ChecksumSHA256": snapshot.checksum_sha256}
        for source in ("upload_response", "exact_head", "ordinary_head"):
            self.assertEqual(validated_s3_proof(proof, version_id="exact", snapshot=snapshot, source=source), {
                "algorithm": "SHA256", "checksum_type": "FULL_OBJECT",
                "checksum": snapshot.checksum_sha256, "version_id": "exact", "source": source,
            })
        self.assertIsNone(validated_s3_proof({"VersionId": "exact"}, version_id="exact", snapshot=snapshot, source="exact_head"))
        with self.assertRaises(UploadProofMismatch):
            validated_s3_proof({**proof, "ChecksumSHA256": "wrong"}, version_id="exact", snapshot=snapshot, source="exact_head")

    def test_unsupported_checksum_requires_explicit_HEAD_capability_evidence(self):
        from botocore.exceptions import ClientError
        for code in ("NotImplemented", "UnsupportedOperation", "XNotImplemented", "501"):
            with self.subTest(code=code):
                self.assertTrue(unsupported_checksum_api(ClientError({"Error": {"Code": code}}, "HeadObject")))
        for code in ("InvalidRequest", "InvalidArgument"):
            for argument in (None, "Bucket", "VersionId"):
                with self.subTest(code=code, argument=argument):
                    error = {"Code": code, "Message": "Request mentioning ChecksumMode is invalid"}
                    if argument is not None:
                        error["ArgumentName"] = argument
                    self.assertFalse(unsupported_checksum_api(ClientError({"Error": error}, "HeadObject")))
            for argument in ("ChecksumMode", "x-amz-checksum-mode"):
                self.assertTrue(unsupported_checksum_api(ClientError({"Error": {
                    "Code": code, "ArgumentName": argument,
                }}, "HeadObject")))
        for code in ("AccessDenied", "NoSuchBucket", "NoSuchVersion", "SlowDown"):
            self.assertFalse(unsupported_checksum_api(ClientError({"Error": {
                "Code": code, "ArgumentName": "ChecksumMode",
            }}, "HeadObject")))
        self.assertFalse(unsupported_checksum_api(ClientError({"Error": {"Code": "NotImplemented"}}, "PutObject")))
        self.assertFalse(unsupported_checksum_api(RuntimeError("unsupported checksum")))

    def test_linked_digest_uses_immutable_AV_snapshot_without_originating_Job(self):
        from app.services.local_files import _require_linked_upload_digest
        snapshot = self.snapshot()
        self.assertEqual(_require_linked_upload_digest({"integrity": "unverified", "upload_snapshot_json": snapshot.as_json()}), snapshot.plaintext_sha256)
        with self.assertRaises(RuntimeError):
            _require_linked_upload_digest({"integrity": "unverified", "upload_snapshot_json": snapshot.as_json(), "upload_plaintext_sha256": "f" * 64})
        with self.assertRaises(RuntimeError):
            _require_linked_upload_digest({"integrity": "unverified", "local_sha256": snapshot.plaintext_sha256})

    def test_invalid_durable_evidence_fails_closed(self):
        import json
        baseline = json.loads(self.snapshot().as_json())
        for change in ({"checksum_sha256": "wrong"}, {"part_checksums": []}, {"algorithm": "CRC32"}, {"size": -1}, {"format": 2}):
            with self.assertRaises(ValueError):
                UploadSnapshot.from_json(json.dumps({**baseline, **change}))

    def test_hash_reads_bounded_chunks_without_materializing_whole_file(self):
        self.path.write_bytes(b"x" * (3 * CHUNK_SIZE + 7))
        real_fdopen = __import__("os").fdopen
        amounts = []
        class TrackingFile:
            def __init__(self, stream): self.stream = stream
            def read(self, amount):
                amounts.append(amount)
                assert 0 < amount <= CHUNK_SIZE
                return self.stream.read(amount)
            def fileno(self): return self.stream.fileno()
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
        with patch("app.services.upload_snapshot.os.fdopen", side_effect=lambda *a, **kw: TrackingFile(real_fdopen(*a, **kw))):
            self.snapshot()
        self.assertGreater(len(amounts), 3)


class SHA256ManagedUploadTests(UploadSnapshotTests):
    def setUp(self):
        super().setUp()
        from botocore.config import Config
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test", config=Config(retries={"mode": "standard", "total_max_attempts": 1}))

    def upload(self, snapshot, **kwargs):
        return upload_file(self.client, source=self.path, bucket="bucket", key="key", snapshot=snapshot,
                           check_active=kwargs.pop("check_active", lambda: None), on_progress=lambda _: None, **kwargs)

    def test_sha256_put_raw_ack_precedes_proof_errors_and_preserves_metadata(self):
        snapshot = self.snapshot()
        for checksum in (None, "wrong", snapshot.checksum_sha256):
            response = {"VersionId": "exact"}
            if checksum is not None: response["ChecksumSHA256"] = checksum
            ack = Mock()
            with Stubber(self.client) as stub:
                stub.add_response("put_object", response, {"Bucket": "bucket", "Key": "key", "Body": ANY,
                                  "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": snapshot.checksum_sha256,
                                  "Metadata": {"opaque": "token"}})
                self.assertEqual(self.upload(snapshot, metadata={"opaque": "token"}, on_acknowledged=ack), response)
                ack.assert_called_once_with(response)
                stub.assert_no_pending_responses()

    def test_definitive_auth_refusal_is_distinct_from_an_ambiguous_send(self):
        from botocore.exceptions import ClientError
        snapshot = self.snapshot()
        with Stubber(self.client) as stub:
            stub.add_client_error('put_object',service_error_code='InvalidAccessKeyId',http_status_code=403)
            with self.assertRaises(ClientError) as caught:
                self.upload(snapshot)
            self.assertEqual(getattr(caught.exception,'upload_no_write_operation',None),'PutObject')
            self.assertFalse(hasattr(caught.exception,'upload_response'))
            stub.assert_no_pending_responses()

    def test_multipart_auth_refusal_before_completion_is_a_durable_no_write_candidate(self):
        from botocore.exceptions import ClientError
        from boto3.s3.transfer import TransferConfig
        snapshot = self.snapshot(b'a' * MULTIPART_THRESHOLD + b'tail')
        for refused_operation in ('create_multipart_upload','complete_multipart_upload'):
            with self.subTest(operation=refused_operation), Stubber(self.client) as stub:
                if refused_operation == 'complete_multipart_upload':
                    stub.add_response('create_multipart_upload',{'UploadId':'mpu'})
                    for number,checksum in enumerate(snapshot.part_checksums,1):
                        stub.add_response('upload_part',{'ETag':f'part-{number}','ChecksumSHA256':checksum})
                stub.add_client_error(refused_operation,service_error_code='AccessDenied',http_status_code=403)
                if refused_operation == 'complete_multipart_upload':
                    stub.add_response('abort_multipart_upload',{})
                with patch('app.services.s3_upload.TransferConfig',return_value=TransferConfig(max_concurrency=1)):
                    with self.assertRaises(ClientError) as caught:
                        self.upload(snapshot)
                self.assertEqual(caught.exception.upload_no_write_operation,
                    'CompleteMultipartUpload' if refused_operation == 'complete_multipart_upload' else 'CreateMultipartUpload')
                self.assertFalse(hasattr(caught.exception,'upload_response'))
                stub.assert_no_pending_responses()

    def test_auth_error_after_acknowledgment_never_confers_no_write(self):
        from botocore.exceptions import ClientError
        snapshot = self.snapshot()
        refused = ClientError({'Error': {'Code':'AccessDenied'},'ResponseMetadata':{'HTTPStatusCode':403}},'PutObject')
        with Stubber(self.client) as stub:
            stub.add_response('put_object',{'VersionId':'accepted','ChecksumSHA256':snapshot.checksum_sha256})
            with self.assertRaises(ClientError) as caught:
                self.upload(snapshot,on_acknowledged=Mock(side_effect=refused))
            self.assertFalse(hasattr(caught.exception,'upload_no_write_operation'))
            self.assertEqual(caught.exception.upload_response['VersionId'],'accepted')

    def test_ack_hook_and_exception_retain_version_before_cancellation(self):
        snapshot = self.snapshot()
        ack = Mock(side_effect=OperationCancelled("lost claim"))
        with Stubber(self.client) as stub:
            stub.add_response("put_object", {"VersionId": "acknowledged", "ChecksumSHA256": "wrong"})
            with self.assertRaises(OperationCancelled) as caught:
                self.upload(snapshot, on_acknowledged=ack)
            self.assertEqual(caught.exception.upload_response["VersionId"], "acknowledged")
            ack.assert_called_once()
            stub.assert_no_pending_responses()

    def test_cancel_before_multipart_response_retains_late_ack_after_real_manager_shutdown(self):
        snapshot = self.snapshot(b"a" * MULTIPART_THRESHOLD + b"tail")
        entered = threading.Event()
        release = threading.Event()
        cancelled = OperationCancelled("cancelled before provider response")
        ack = Mock()
        real_complete = self.client.complete_multipart_upload
        from s3transfer.manager import TransferManager as RealManager

        def delayed_complete(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("shutdown never released the provider")
            return real_complete(**kwargs)

        def active():
            if entered.is_set():
                raise cancelled

        def manager_factory(*args, **kwargs):
            manager = RealManager(*args, **kwargs)
            shutdown = manager.shutdown
            def shutdown_with_late_response(*args, **kwargs):
                release.set()
                return shutdown(*args, **kwargs)
            manager.shutdown = shutdown_with_late_response
            return manager

        with Stubber(self.client) as stub, patch.object(self.client, "complete_multipart_upload", side_effect=delayed_complete), patch(
            "app.services.s3_upload.TransferManager", side_effect=manager_factory,
        ):
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            for number in range(2):
                stub.add_response("upload_part", {"ETag": f"part-{number}"})
            stub.add_response("complete_multipart_upload", {"VersionId": "late-response", "ChecksumSHA256": snapshot.checksum_sha256})
            with self.assertRaises(OperationCancelled) as caught:
                self.upload(snapshot, check_active=active, on_acknowledged=ack)
            self.assertIs(caught.exception, cancelled)
            self.assertEqual(caught.exception.upload_response["VersionId"], "late-response")
            ack.assert_called_once()
            stub.assert_no_pending_responses()

    def test_multipart_sha256_composite_request_uses_independent_expected_parts(self):
        snapshot = self.snapshot(b"a" * MULTIPART_THRESHOLD + b"tail")
        with Stubber(self.client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"},
                              {"Bucket": "bucket", "Key": "key", "ChecksumAlgorithm": "SHA256", "ChecksumType": "COMPOSITE"})
            # Serialize only for deterministic Stubber response ordering.
            for number, checksum in enumerate(snapshot.part_checksums, 1):
                stub.add_response("upload_part", {"ETag": f"etag{number}", "ChecksumSHA256": checksum},
                                  {"Bucket": "bucket", "Key": "key", "UploadId": "mpu", "PartNumber": number,
                                   "Body": ANY, "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": checksum})
            stub.add_response("complete_multipart_upload", {"VersionId": "exact-mpu", "ChecksumType": "COMPOSITE", "ChecksumSHA256": snapshot.checksum_sha256},
                              {"Bucket": "bucket", "Key": "key", "UploadId": "mpu", "ChecksumType": "COMPOSITE",
                               "ChecksumSHA256": snapshot.checksum_sha256, "MpuObjectSize": snapshot.size,
                               "MultipartUpload": {"Parts": [{"ETag": f"etag{n}", "PartNumber": n, "ChecksumSHA256": c} for n,c in enumerate(snapshot.part_checksums,1)]}})
            from boto3.s3.transfer import TransferConfig
            with patch("app.services.s3_upload.TransferConfig", return_value=TransferConfig(max_concurrency=1)):
                self.assertEqual(self.upload(snapshot)["VersionId"], "exact-mpu")
            stub.assert_no_pending_responses()

    def test_multipart_A_to_B_unread_part_is_rejected_and_aborted_without_completion(self):
        snapshot = self.snapshot(b"a" * MULTIPART_THRESHOLD + b"tail")
        real_part = self.client.upload_part
        def part(**kwargs):
            result = real_part(**kwargs)
            if kwargs["PartNumber"] == 1:
                with self.path.open("r+b") as stream:
                    stream.seek(MULTIPART_THRESHOLD)
                    stream.write(b"evil")
            return result
        with Stubber(self.client) as stub, patch.object(self.client, "upload_part", side_effect=part):
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            stub.add_response("upload_part", {"ETag": "part-1", "ChecksumSHA256": snapshot.part_checksums[0]})
            stub.add_response("abort_multipart_upload", {}, {"Bucket": "bucket", "Key": "key", "UploadId": "mpu"})
            from boto3.s3.transfer import TransferConfig
            with patch("app.services.s3_upload.TransferConfig", return_value=TransferConfig(max_concurrency=1)):
                with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                    self.upload(snapshot)
            stub.assert_no_pending_responses()

    def test_multipart_fifo_race_fails_without_blocking_a_file_open_and_aborts(self):
        import builtins
        import os
        snapshot = self.snapshot(b"a" * MULTIPART_THRESHOLD)
        real_create = self.client.create_multipart_upload
        real_open = builtins.open
        def create(**kwargs):
            result = real_create(**kwargs)
            self.path.unlink()
            os.mkfifo(self.path)
            return result
        def guarded_open(path, mode="r", *args, **kwargs):
            # Keep the RED test from hanging on the old blocking filename path.
            if str(path) == str(self.path):
                raise AssertionError("blocking open would hang on replacement FIFO")
            return real_open(path, mode, *args, **kwargs)
        with Stubber(self.client) as stub, patch.object(self.client, "create_multipart_upload", side_effect=create), patch(
            "builtins.open", side_effect=guarded_open,
        ):
            stub.add_response("create_multipart_upload", {"UploadId": "mpu"})
            stub.add_response("abort_multipart_upload", {})
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                self.upload(snapshot)
            stub.assert_no_pending_responses()

    def test_A_to_B_between_hashing_and_transport_cannot_send_B(self):
        snapshot = self.snapshot()
        self.path.write_bytes(b"snapshot B")
        with patch.object(self.client, "put_object") as put:
            with self.assertRaisesRegex(RuntimeError, "changed since fingerprinting"):
                self.upload(snapshot)
            put.assert_not_called()

    def test_buffered_A_is_sent_even_if_source_changes_during_provider_read(self):
        snapshot = self.snapshot()
        real_put = self.client.put_object
        def put(**kwargs):
            self.path.write_bytes(b"snapshot B")
            self.assertEqual(kwargs["Body"].read(), b"snapshot A")
            kwargs["Body"].seek(0)
            self.assertEqual(kwargs["Body"].read(3), b"sna")
            return real_put(**kwargs)
        with Stubber(self.client) as stub, patch.object(self.client, "put_object", side_effect=put):
            stub.add_response("put_object", {"VersionId": "A", "ChecksumSHA256": snapshot.checksum_sha256})
            self.assertEqual(self.upload(snapshot)["VersionId"], "A")

    def test_validated_transport_body_retains_real_shared_budget_cancellation_and_progress(self):
        snapshot = self.snapshot()
        coordinator = TransferCoordinator()
        body = ReadFileChunk(_shared_budget(1024**3).get_bandwith_limited_stream(InterruptReader(io.BytesIO(b"snapshot A"), coordinator), coordinator, enabled=False), snapshot.size, snapshot.size, callbacks=[])
        facade = _UploadClient(self.client, snapshot=snapshot, max_bandwidth=1024**3)
        wrapped = facade._validated_body(body, 1)
        self.addCleanup(lambda: (wrapped.signal_not_transferring(), wrapped.close()))
        self.assertIs(wrapped._fileobj._leaky_bucket, _shared_budget(1024**3)._leaky_bucket)
        progress = Mock()
        wrapped._callbacks = [progress]
        wrapped.signal_transferring()
        self.assertEqual(wrapped.read(3), b"sna")
        progress.assert_called_with(bytes_transferred=3)
        coordinator.set_exception(OperationCancelled("cancelled"))
        with self.assertRaises(OperationCancelled): wrapped.read(1)

    def test_cancel_during_bounded_buffering_stops_before_put(self):
        snapshot = self.snapshot(b"a" * (2 * CHUNK_SIZE))
        checks = 0
        def active():
            nonlocal checks
            checks += 1
            if checks > 2: raise OperationCancelled("stopped")
        with patch.object(self.client, "put_object") as put:
            with self.assertRaises(OperationCancelled): self.upload(snapshot, check_active=active)
            put.assert_not_called()
