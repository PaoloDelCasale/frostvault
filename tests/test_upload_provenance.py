"""Provider-side fail-closed boundaries (no DB required)."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import boto3
from botocore.config import Config
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from types import SimpleNamespace
from app.services.upload_provenance import ProvenAbsent, UploadBlocked, TOKEN_KEY, inventory, provider_identity, reconcile, receipt
from app.services.upload_snapshot import prepare_upload_snapshot
from app.services.s3_upload import _UploadClient
from app.services.s3_copy import _object_version_entries
from tests.upload_provenance_provider import VersionedProvider, provider_attempt


class ProvenanceLockModeTests(unittest.TestCase):
    def test_target_is_exclusive_by_default_and_share_is_explicit(self):
        from app.services import upload_provenance as provenance
        for kwargs, expected in (({}, "FOR NO KEY UPDATE"), ({"shared": True}, "FOR SHARE")):
            with self.subTest(kwargs=kwargs):
                connection = MagicMock()
                connection.execute.return_value.fetchone.return_value = None
                with self.assertRaises(UploadBlocked):
                    provenance.target(connection, {"id": 1, "vault_id": 2, "file_id": "file"},
                        key="key", provider="fake", **kwargs)
                self.assertEqual(connection.execute.call_args_list[0].args[0],
                    "SELECT * FROM vaults WHERE id=%s " + expected)

    def test_nested_target_checks_keep_the_requested_mode(self):
        from app.services import upload_provenance as provenance
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file"
            source.write_bytes(b"content")
            snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        attempt = provider_attempt(snapshot, token="token", provider="fake", send_started=False)
        pinned = attempt.target
        bound = (attempt.admitted, False)
        job = {"id": 1}
        for shared in (False, True):
            # Every nested check reads the target and its locked Vault row
            # through ``_target``; each must keep the requested mode.
            with self.subTest(shared=shared), \
                    patch.object(provenance, "_target", return_value=(pinned, {})) as target, \
                    patch.object(provenance, "_bound", return_value=bound), \
                    patch.object(provenance, "_open_attempt", return_value=attempt):
                connection = MagicMock()
                provenance.require_target(connection, job, pinned, shared=shared)
                provenance.prepare(connection, job, pinned=pinned, snapshot=snapshot,
                    requested="s3_checksum", local_generation=1, before={}, timestamp="now",
                    shared=shared)
                provenance.mark_send_started(connection, job, attempt, shared=shared)
                self.assertEqual(target.call_count, 3)
                self.assertTrue(all(call.kwargs["shared"] is shared for call in target.call_args_list))


class ProvenanceProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = VersionedProvider()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        source = Path(self.directory.name) / "file"
        source.write_bytes(b"independent A")
        self.snapshot, _ = prepare_upload_snapshot(source, lambda: None)
        self.intent = provider_attempt(self.snapshot, token="opaque-attempt",
                                       provider=provider_identity(self.provider))

    def test_receipts_combine_and_contradictions_never_resolve_by_choice(self):
        connection = MagicMock()
        def rows(responses, outcome=None, landed=None):
            connection.execute.return_value.fetchall.return_value = [
                {"response": r, "outcome": outcome, "landed_version_id": landed} for r in responses]
        rows([])
        self.assertIsNone(receipt(connection, "attempt"))
        rows([{"VersionId": "exact"}, {"VersionId": "exact", "ETag": "e"}])
        self.assertEqual(receipt(connection, "attempt"), {"VersionId": "exact", "ETag": "e"})
        rows([{"VersionId": "exact"}], outcome="landed", landed="exact")
        self.assertEqual(receipt(connection, "attempt"), {"VersionId": "exact"})
        for responses, outcome, landed in (
            ([{"VersionId": "null"}], None, None),
            ([{}], None, None),
            ([{"VersionId": "a"}, {"VersionId": "b"}], None, None),
            # A late receipt contradicts a settlement, however old.
            ([{"VersionId": "late"}], "no_write", None),
            ([{"VersionId": "late"}], "unsent", None),
            ([{"VersionId": "other"}], "landed", "exact"),
        ):
            rows(responses, outcome, landed)
            with self.subTest(responses=responses, outcome=outcome), self.assertRaises(UploadBlocked) as blocked:
                receipt(connection, "attempt")
            self.assertEqual(blocked.exception.reason, "ambiguous_receipt")

    def test_unique_token_reconciles_exact_identity_against_independent_snapshot(self):
        self.provider.add("key", b"independent A", {TOKEN_KEY: self.intent.token}, version="exact")
        result = reconcile(self.provider, self.intent, None, lambda: None)
        self.assertEqual(result["VersionId"], "exact")
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (0, 0, 0))

    def test_zero_multiple_and_false_token_preserve_all_objects(self):
        for scenario in ("zero", "multiple", "false"):
            with self.subTest(scenario=scenario):
                provider = VersionedProvider()
                if scenario != "zero":
                    provider.add("key", b"wrong content" if scenario == "false" else b"independent A", {TOKEN_KEY: self.intent.token}, version="one")
                if scenario == "multiple":
                    provider.add("key", b"independent A", {TOKEN_KEY: self.intent.token}, version="two")
                original = dict(provider.objects)
                with self.assertRaises(UploadBlocked):
                    reconcile(provider, self.intent, None, lambda: None)
                self.assertEqual(provider.objects, original)
                self.assertEqual((provider.puts, provider.deletes), (0, 0))

    def test_complete_empty_listings_and_no_incomplete_multipart_prove_the_send_never_landed(self):
        with self.assertRaises(ProvenAbsent):
            reconcile(self.provider, self.intent, None, lambda: None)
        self.assertGreaterEqual(self.provider.multipart_listings, 1)
        self.assertEqual((self.provider.puts, self.provider.gets, self.provider.deletes), (0, 0, 0))

    def test_absence_is_never_proven_when_it_cannot_be_positively_read(self):
        scenarios = {}
        incomplete = VersionedProvider()
        incomplete.multiparts.append(("key", "upload-1"))
        scenarios["incomplete multipart for the key"] = incomplete
        no_api = VersionedProvider()
        no_api.list_multipart_uploads = None
        scenarios["multipart listing unsupported"] = no_api
        unconfirmed = VersionedProvider()
        unconfirmed.list_multipart_uploads = lambda **kwargs: {"Uploads": []}
        scenarios["pagination not confirmed"] = unconfirmed
        for name, provider in scenarios.items():
            with self.subTest(scenario=name):
                with self.assertRaises(UploadBlocked) as blocked:
                    reconcile(provider, self.intent, None, lambda: None)
                self.assertNotIsInstance(blocked.exception, ProvenAbsent)
                self.assertEqual(blocked.exception.reason, "ambiguous_send")
                self.assertEqual((provider.puts, provider.deletes), (0, 0))

    def test_absence_requires_the_pinned_inventory_and_no_receipt(self):
        for name, prepare, saved in (
            ("older version outside the pinned inventory", lambda p: p.add("key", b"other", version="external"), None),
            ("receipt version missing from the listing", lambda p: None, {"VersionId": "lost"}),
        ):
            with self.subTest(case=name):
                provider = VersionedProvider()
                prepare(provider)
                with self.assertRaises(UploadBlocked) as blocked:
                    reconcile(provider, self.intent, saved, lambda: None)
                self.assertNotIsInstance(blocked.exception, ProvenAbsent)

    def test_connectivity_failure_reading_the_inventory_is_not_reported_as_a_changed_inventory(self):
        from botocore.exceptions import EndpointConnectionError
        error = EndpointConnectionError(endpoint_url="https://s3.invalid")
        with patch.object(self.provider, "list_object_versions", side_effect=error):
            with self.assertRaises(EndpointConnectionError):
                inventory(self.provider, "bucket", "key")
        # An incomplete or malformed answer is an unconfirmed inventory.
        for page in ({"Versions": []}, {"IsTruncated": False, "Versions": "garbled"}, ["garbled"]):
            with self.subTest(page=page), patch.object(self.provider, "list_object_versions", return_value=page):
                with self.assertRaises(UploadBlocked) as blocked:
                    inventory(self.provider, "bucket", "key")
                self.assertEqual(blocked.exception.reason, "inventory_changed")

    def test_a_programming_error_reading_the_inventory_is_a_worker_error_not_a_block(self):
        # #628: no S3 client at all is FrostVault's bug, never a changed inventory.
        with self.assertRaises(AttributeError):
            inventory(None, "bucket", "key")
        for error in (TypeError("bad call"), ValueError("bad value"), KeyError("Versions")):
            with self.subTest(error=repr(error)), \
                    patch.object(self.provider, "list_object_versions", side_effect=error):
                with self.assertRaises(type(error)):
                    inventory(self.provider, "bucket", "key")

    def test_inflight_own_write_between_complete_listings_reconciles_automatically(self):
        listing = self.provider.list_object_versions
        calls = []
        def finish_write(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.provider.add('key', b'independent A', {TOKEN_KEY: self.intent.token}, version='own-late')
            return listing(**kwargs)
        with patch.object(self.provider, 'list_object_versions', side_effect=finish_write):
            result = reconcile(self.provider, self.intent, None, lambda: None)
        self.assertEqual(result['VersionId'], 'own-late')
        self.assertGreaterEqual(len(calls), 4, 'Must obtain two fresh complete stable listings')
        self.assertEqual((self.provider.puts, self.provider.deletes), (0, 0))

    def test_late_token_without_independent_proof_never_authorizes_auto_reobserve(self):
        listing = self.provider.list_object_versions
        head = self.provider.head_object
        calls = []
        def finish_write(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.provider.add('key', b'independent A', {TOKEN_KEY: self.intent.token}, version='own-late')
            return listing(**kwargs)
        def no_proof(**kwargs):
            result = head(**kwargs)
            result.pop('ChecksumSHA256')
            result.pop('ChecksumType')
            return result
        with patch.object(self.provider,'list_object_versions',side_effect=finish_write), patch.object(
            self.provider,'head_object',side_effect=no_proof
        ), self.assertRaises(UploadBlocked) as blocked:
            reconcile(self.provider,self.intent,None,lambda: None)
        self.assertEqual(blocked.exception.reason,'inventory_changed')
        self.assertEqual(len(calls),2,'Token/size alone must not trigger automatic re-observation')
        self.assertEqual(self.provider.deletes,0)

    def test_unsupported_checksum_head_does_not_turn_token_into_auto_retry_authority(self):
        listing = self.provider.list_object_versions
        calls = []
        def finish_write(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.provider.add('key',b'independent A',{TOKEN_KEY:self.intent.token},version='own-late')
            return listing(**kwargs)
        unsupported = ClientError({'Error': {'Code':'NotImplemented'}},'HeadObject')
        with patch.object(self.provider,'list_object_versions',side_effect=finish_write), patch.object(
            self.provider,'head_object',side_effect=unsupported
        ), self.assertRaises(UploadBlocked) as blocked:
            reconcile(self.provider,self.intent,None,lambda: None)
        self.assertEqual(blocked.exception.reason,'inventory_changed')
        self.assertIn('Retry failed uploads',blocked.exception.remedy)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.provider.deletes,0)

    def test_owned_arrival_does_not_allow_a_later_external_delta_or_marker(self):
        for marker in (False,True):
            provider = VersionedProvider()
            listing = provider.list_object_versions
            calls = []
            def finish_then_external(**kwargs):
                calls.append(1)
                if len(calls) == 2:
                    provider.add('key',b'independent A',{TOKEN_KEY:self.intent.token},version='own-late')
                if len(calls) == 3:
                    if marker:
                        provider.markers['key','external'] = True
                    else:
                        provider.add('key',b'independent A',version='external')
                return listing(**kwargs)
            with self.subTest(marker=marker), patch.object(provider,'list_object_versions',side_effect=finish_then_external):
                with self.assertRaises(UploadBlocked) as blocked:
                    reconcile(provider,self.intent,None,lambda: None)
                self.assertEqual(blocked.exception.reason,'inventory_changed')
                self.assertEqual((provider.puts,provider.deletes),(0,0))

    def test_inflight_external_write_between_listings_remains_actionable_block(self):
        listing = self.provider.list_object_versions
        calls = []
        self.provider.add('key', b'independent A', {TOKEN_KEY: self.intent.token}, version='own')
        def external_write(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.provider.add('key', b'independent A', version='external-late')
            return listing(**kwargs)
        with patch.object(self.provider, 'list_object_versions', side_effect=external_write):
            with self.assertRaises(UploadBlocked) as blocked:
                reconcile(self.provider, self.intent, None, lambda: None)
        self.assertEqual(blocked.exception.reason, 'inventory_changed')
        self.assertIn('Retry failed uploads', blocked.exception.remedy)
        self.assertNotIn('stop external S3 writers', blocked.exception.remedy)
        self.assertEqual(self.provider.deletes, 0)

    def test_raw_proof_cannot_hide_contradictory_head(self):
        self.provider.add("key", b"independent B", {TOKEN_KEY: self.intent.token}, version="exact")
        with self.assertRaises(UploadBlocked):
            reconcile(self.provider, self.intent, {"VersionId": "exact", "ChecksumSHA256": self.snapshot.checksum_sha256}, lambda: None)

    def test_external_writer_or_marker_blocks_even_identical_bytes(self):
        for marker in (False, True):
            with self.subTest(marker=marker):
                self.provider.objects.clear()
                self.provider.markers.clear()
                self.provider.add("key", b"independent A", {TOKEN_KEY: self.intent.token}, version="exact")
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
