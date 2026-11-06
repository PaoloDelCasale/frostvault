"""Provider-side fail-closed boundaries (no DB required)."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.config import Config
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from types import SimpleNamespace
from app.services.upload_provenance import UploadBlocked, TOKEN_KEY, canonical, inventory, provider_identity, reconcile
from app.services.snapshot_cleanup import exact_duplicate
from app.services.upload_snapshot import prepare_upload_snapshot
from app.services.s3_upload import _UploadClient
from app.services.s3_copy import _object_version_entries
from tests.upload_provenance_provider import VersionedProvider


class ProvenanceProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = VersionedProvider()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        source = Path(self.directory.name) / "file"
        source.write_bytes(b"independent A")
        self.snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        self.intent = {"token": "opaque-attempt", "target_json": canonical({"key": "key", "bucket": "bucket", "provider": provider_identity(self.provider)}), "inventory_json": canonical({"versions": [], "markers": []}), "snapshot_json": self.snapshot.as_json()}

    def test_unique_token_reconciles_exact_identity_against_independent_snapshot(self):
        self.provider.add("key", b"independent A", {TOKEN_KEY: self.intent["token"]}, version="exact")
        result = reconcile(self.provider, self.intent, None, lambda: None)
        self.assertEqual(result["VersionId"], "exact")
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (0, 0, 0))

    def test_zero_multiple_and_false_token_preserve_all_objects(self):
        for scenario in ("zero", "multiple", "false"):
            with self.subTest(scenario=scenario):
                provider = VersionedProvider()
                if scenario != "zero":
                    provider.add("key", b"wrong content" if scenario == "false" else b"independent A", {TOKEN_KEY: self.intent["token"]}, version="one")
                if scenario == "multiple":
                    provider.add("key", b"independent A", {TOKEN_KEY: self.intent["token"]}, version="two")
                original = dict(provider.objects)
                with self.assertRaises(UploadBlocked):
                    reconcile(provider, self.intent, None, lambda: None)
                self.assertEqual(provider.objects, original)
                self.assertEqual((provider.puts, provider.deletes), (0, 0))

    def test_raw_proof_cannot_hide_contradictory_head(self):
        self.provider.add("key", b"independent B", {TOKEN_KEY: self.intent["token"]}, version="exact")
        with self.assertRaises(UploadBlocked):
            reconcile(self.provider, self.intent, {"VersionId": "exact", "ChecksumSHA256": self.snapshot.checksum_sha256}, lambda: None)

    def test_external_writer_or_marker_blocks_even_identical_bytes(self):
        for marker in (False, True):
            with self.subTest(marker=marker):
                self.provider.objects.clear()
                self.provider.markers.clear()
                self.provider.add("key", b"independent A", {TOKEN_KEY: self.intent["token"]}, version="exact")
                if marker:
                    self.provider.markers["key", "marker"] = True
                else:
                    self.provider.add("key", b"independent A", version="external")
                with self.assertRaises(UploadBlocked):
                    reconcile(self.provider, self.intent, None, lambda: None)
                self.assertEqual(self.provider.deletes, 0)

    def test_complete_inventory_rejects_loops_null_and_duplicate_ids(self):
        cases = [
            ({"IsTruncated": True, "NextKeyMarker": "key", "NextVersionIdMarker": "same"}, "repeated its continuation marker"),
            ({"IsTruncated": False, "Versions": [{"Key": "key", "VersionId": "null"}]}, "ambiguous exact identities"),
            ({"IsTruncated": False, "Versions": [{"Key": "key", "VersionId": "dup"}] * 2}, "ambiguous exact identities"),
            ({"IsTruncated": False, "Versions": [{"Key": "key", "VersionId": "dup"}], "DeleteMarkers": [{"Key": "key", "VersionId": "dup"}]}, "ambiguous exact identities"),
        ]
        for page, reason in cases:
            with self.subTest(page=page), patch.object(self.provider, "list_object_versions", return_value=page):
                with self.assertRaisesRegex(RuntimeError, reason):
                    _object_version_entries(self.provider, bucket="bucket", object_key="key")

    def test_historical_exact_get_requires_identity_length_digest_and_no_token(self):
        self.provider.add("key", b"independent A", version="duplicate")
        exact_duplicate(self.provider, bucket="bucket", key="key", version="duplicate", digest=self.snapshot.plaintext_sha256, size=self.snapshot.size, check_active=lambda: None)
        for response in (
            {"VersionId": "other", "ContentLength": self.snapshot.size},
            {"VersionId": "duplicate", "ContentLength": self.snapshot.size + 1},
            {"VersionId": "duplicate", "ContentLength": self.snapshot.size, "Metadata": {TOKEN_KEY: "fake"}},
        ):
            body = io.BytesIO(b"independent A")
            with self.subTest(response=response), patch.object(self.provider, "get_object", return_value={**response, "Body": body}):
                with self.assertRaises(UploadBlocked):
                    exact_duplicate(self.provider, bucket="bucket", key="key", version="duplicate", digest=self.snapshot.plaintext_sha256, size=self.snapshot.size, check_active=lambda: None)
            self.assertTrue(body.closed)
        self.assertEqual(self.provider.deletes, 0)

    def test_historical_different_same_length_and_truncated_are_retained(self):
        for data in (b"independent B", b"short"):
            with self.subTest(data=data):
                self.provider.add("key", data, version="different")
                with self.assertRaises(UploadBlocked):
                    exact_duplicate(self.provider, bucket="bucket", key="key", version="different", digest=self.snapshot.plaintext_sha256, size=self.snapshot.size, check_active=lambda: None)
                self.assertIn(("key", "different"), self.provider.objects)

    def test_actual_botocore_mutator_failures_do_not_retry_hidden_put_or_completion(self):
        read = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test", config=Config(retries={"mode":"standard","total_max_attempts":4}))
        write = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test", config=Config(retries={"mode":"standard","total_max_attempts":1}))
        facade = _UploadClient(read, write_client=write)
        def unavailable(_request):
            return AWSResponse("https://disposable.invalid",503,{"content-type":"application/xml"},SimpleNamespace(stream=lambda **_: iter([b"<Error><Code>ServiceUnavailable</Code></Error>"])))
        for method, args in ((facade.put_object,{"Bucket":"bucket","Key":"key","Body":b"A"}), (facade.create_multipart_upload,{"Bucket":"bucket","Key":"key"}), (facade.complete_multipart_upload,{"Bucket":"bucket","Key":"key","UploadId":"same-mpu","MultipartUpload":{"Parts":[{"PartNumber":1,"ETag":"part"}]}})):
            with self.subTest(method=method.__name__), patch.object(write._endpoint,"_send",side_effect=unavailable) as send:
                with self.assertRaises(ClientError): method(**args)
                self.assertEqual(send.call_count,1,"Botocore retried a non-idempotent write")
        self.assertEqual(read.meta.config.retries["total_max_attempts"],4)

    def test_non_idempotent_sdk_requires_one_shot_configuration(self):
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test", config=Config(retries={"mode": "standard", "total_max_attempts": 4}))
        with self.assertRaisesRegex(RuntimeError, "total_max_attempts=1"):
            _UploadClient(client)
        one_shot = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test", config=Config(retries={"mode": "standard", "total_max_attempts": 1}))
        facade = _UploadClient(client, write_client=one_shot)
        self.assertEqual(facade.client.meta.config.retries["total_max_attempts"], 4)
        self.assertEqual(facade.write_client.meta.config.retries["total_max_attempts"], 1)
