"""Pure provider/eligibility boundaries; no database, network or credentials (#595)."""
import base64
import hashlib
import io
import unittest
from unittest.mock import Mock, patch

from types import SimpleNamespace

from app.services import copy_provenance, s3_copy, upload_provenance as provenance, vault_moves as moves
from app.services.s3_copy import COPY_TOKEN_KEY
from tests.upload_provenance_provider import VersionedProvider


VAULT = {'s3_bucket': 'bucket', 'uuid': '11111111-1111-1111-1111-111111111111'}


class MoveEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(storage_class="STANDARD", busy=False, integrity="verified", availability="available",
                        plaintext_sha256="a"*64, size=1, provider_version_id="source", restore_state=None)

    def test_only_strictly_colder_supported_transitions(self):
        self.assertIsNone(moves.eligibility(self.row, "DEEP_ARCHIVE"))
        for source,target in (("DEEP_ARCHIVE","STANDARD"),("GLACIER_IR","STANDARD_IA"),
                              ("GLACIER","GLACIER"),("UNKNOWN","DEEP_ARCHIVE")):
            self.assertEqual(moves.eligibility({**self.row,"storage_class":source},target),"already_equal_or_colder")

    def test_pending_deletion_upload_or_recovery_and_unverified_copies_are_skipped(self):
        self.assertEqual(moves.eligibility({**self.row,"busy":True},"GLACIER_IR"),"pending_work")
        for key,value in (("integrity","unverified"),("integrity","mismatch"),("availability","missing"),
                          ("provider_version_id","null"),("size",None),("plaintext_sha256",None)):
            with self.subTest(key=key,value=value):
                self.assertEqual(moves.eligibility({**self.row,key:value},"GLACIER_IR"),"unverified_or_unavailable")

    def test_cold_source_requires_explicit_restore_and_never_warms(self):
        row = {**self.row,"storage_class":"GLACIER"}
        self.assertEqual(moves.eligibility(row,"DEEP_ARCHIVE"),"explicit_restore_required")
        self.assertIsNone(moves.eligibility({**row,"restore_state":"available"},"DEEP_ARCHIVE"))
        self.assertEqual(moves.eligibility({**row,"restore_state":"available"},"STANDARD"),"already_equal_or_colder")

    def test_mixed_classes_estimate_counts_per_object_and_warns_180_days(self):
        rows = [self.row,{**self.row,"storage_class":"DEEP_ARCHIVE"},{**self.row,"busy":True},
                {**self.row,"storage_class":"GLACIER"}]
        result = moves.estimate(rows,"DEEP_ARCHIVE")
        self.assertEqual(result["eligible_count"],1)
        self.assertEqual(result["total_bytes"],1)
        self.assertEqual(result["minimum_days"],180)
        self.assertEqual(sum(result["skipped"].values()),3)
        self.assertIn("excluded",result["assumptions"])

    def test_stopped_and_reconnecting_vaults_are_fenced(self):
        moves.require_ready(dict(lifecycle="active"))
        for lifecycle in ("reconnect_required","stopping","tombstone"):
            with self.subTest(lifecycle=lifecycle),self.assertRaises(moves.MoveBlocked):
                moves.require_ready(dict(lifecycle=lifecycle))


class ExactCopyTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.job = {"s3_bucket":"bucket"}
        self.move = dict(object_key="vault/photo", source_version_id="source",token="intent",size=3)
        self.checksum = base64.b64encode(hashlib.sha256(b"abc").digest()).decode()

    def test_explicit_external_cold_restore_is_observed_without_requesting_one(self):
        row=dict(storage_class='GLACIER',busy=False,integrity='verified',availability='available',
                 plaintext_sha256='a'*64,size=3,provider_version_id='source',object_key='vaults/11111111-1111-1111-1111-111111111111/photo',restore_state='not_requested')
        self.client.head_object.return_value={'VersionId':'source','ContentLength':3,'StorageClass':'GLACIER',
                                             'Restore':'ongoing-request="false", expiry-date="Fri, 06 Nov 2026 12:00:00 GMT"'}
        with patch('app.storage.s3_client',return_value=self.client):
            moves.refresh_cold_sources(VAULT,[row])
        self.assertIsNone(moves.eligibility(row,'DEEP_ARCHIVE'))
        self.assertEqual(self.client.head_object.call_args.kwargs['VersionId'],'source')
        self.client.restore_object.assert_not_called()

    def test_expired_or_unprovable_cold_restore_is_not_admitted_from_cached_flag(self):
        row=dict(storage_class='GLACIER',busy=False,integrity='verified',availability='available',
                 plaintext_sha256='a'*64,size=3,provider_version_id='source',object_key='vaults/11111111-1111-1111-1111-111111111111/photo',restore_state='available')
        self.client.head_object.return_value={'VersionId':'source','ContentLength':3,'StorageClass':'GLACIER'}
        with patch('app.storage.s3_client',return_value=self.client):
            moves.refresh_cold_sources(VAULT,[row])
            self.assertEqual(moves.eligibility(row,'DEEP_ARCHIVE'),'explicit_restore_required')
            self.client.head_object.side_effect=RuntimeError('provider unavailable')
            moves.refresh_cold_sources(VAULT,[row])
            self.assertEqual(moves.eligibility(row,'DEEP_ARCHIVE'),'cloud_check_unavailable')
        self.client.restore_object.assert_not_called()

    def test_small_copy_preserves_metadata_tags_and_exact_source_with_provenance(self):
        self.client.copy_object.return_value = {"VersionId":"dest"}
        result = s3_copy._copy_storage_class_version(self.client,bucket="bucket",object_key="vault/photo",
            source_version_id="source",target_class="GLACIER_IR",source_size=3,
            source_head={"Metadata":{"custom":"kept"},"ContentType":"image/jpeg"},intent_token="intent")
        self.assertEqual(result,"dest")
        kwargs = self.client.copy_object.call_args.kwargs
        self.assertEqual(kwargs["CopySource"]["VersionId"],"source")
        self.assertEqual(kwargs["Metadata"],{"custom":"kept","frostvault-move":"intent"})
        self.assertEqual(kwargs["TaggingDirective"],"COPY")
        self.assertEqual(kwargs["ContentType"],"image/jpeg")

    def _attempt(self, provider):
        source = provider.add("vaults/v/photo", b"abc")
        inventory = provenance.inventory(provider, "bucket", "vaults/v/new")
        return copy_provenance.Attempt(
            id="attempt", purpose="rename", job_id=1, source_version_id="av",
            source_key="vaults/v/photo", source_provider_version_id=source,
            destination_key="vaults/v/new", storage_class="STANDARD", size=3,
            plaintext_sha256=hashlib.sha256(b"abc").digest(), token="intent",
            inventory=inventory, composite_checksum=None, send_started=True, bucket="bucket")

    def _reconcile(self, provider, attempt):
        journal = Mock()
        journal.__enter__ = Mock(return_value=journal)
        journal.__exit__ = Mock(return_value=False)
        journal.execute.return_value.fetchall.return_value = []
        worker = SimpleNamespace(db=lambda: journal)
        return copy_provenance.reconcile(worker, {"id": 1}, attempt, provider, Mock())

    def test_discovery_uses_the_copy_token_not_class_size_or_etag(self):
        provider = VersionedProvider()
        attempt = self._attempt(provider)
        provider.add("vaults/v/new", b"abc", {"frostvault-upload": "intent"})
        with self.assertRaises(provenance.UploadBlocked) as blocked:
            self._reconcile(provider, attempt)
        self.assertEqual(blocked.exception.reason, "ambiguous_send")
        own = provider.add("vaults/v/new", b"abc", {COPY_TOKEN_KEY: "intent"})
        self.assertEqual(self._reconcile(provider, attempt), own)

    def test_duplicate_provider_results_are_ambiguous_never_deleted(self):
        provider = VersionedProvider()
        attempt = self._attempt(provider)
        for _ in range(2):
            provider.add("vaults/v/new", b"abc", {COPY_TOKEN_KEY: "intent"})
        with self.assertRaises(provenance.UploadBlocked) as blocked:
            self._reconcile(provider, attempt)
        self.assertEqual(blocked.exception.reason, "ambiguous_receipt")
        self.assertEqual(provider.deleted, [])

    def test_destination_must_match_known_plaintext_not_just_size_etag(self):
        self.client.head_object.return_value = {"VersionId":"dest","ContentLength":3,"StorageClass":"DEEP_ARCHIVE",
                                              "ChecksumSHA256":"wrong"}
        with self.assertRaisesRegex(RuntimeError,"integrity"):
            s3_copy._verify_storage_class_destination(self.client,bucket="b",object_key="k",source_version_id="source",
                candidate_version_id="dest",target_class="DEEP_ARCHIVE",expected_size=3,
                expected_sha256_checksum=self.checksum)
        self.client.delete_object.assert_not_called()

    def test_composite_proof_is_independent_of_stream_chunk_boundaries(self):
        self.client.get_object.return_value = {"Body":io.BytesIO(b"abcdefghijk")}
        checksum = base64.b64encode(hashlib.sha256(b"abcdefghijk").digest()).decode()
        expected = base64.b64encode(hashlib.sha256(b"".join(hashlib.sha256(p).digest()
            for p in (b"abcd",b"efgh",b"ijk"))).digest()).decode()+"-3"
        with patch.multiple(s3_copy,S3_MULTIPART_COPY_MIN_PART_BYTES=4,S3_MULTIPART_COPY_PART_BYTES=4,S3_OBJECT_HASH_CHUNK_BYTES=3):
            actual = s3_copy._multipart_source_checksum(self.client,bucket="b",object_key="k",version_id="source",
                size=11,expected_sha256=checksum,check_active=Mock())
        self.assertEqual(actual,expected)
        self.assertEqual(self.client.get_object.call_args.kwargs["VersionId"],"source")

    def test_multipart_source_mismatch_fails_before_copy(self):
        self.client.get_object.return_value = {"Body":io.BytesIO(b"bad")}
        with self.assertRaisesRegex(RuntimeError,"plaintext digest"):
            s3_copy._multipart_source_checksum(self.client,bucket="b",object_key="k",version_id="source",
                size=3,expected_sha256=self.checksum,check_active=Mock())
        self.client.create_multipart_upload.assert_not_called()

    def test_cancelled_multipart_aborts_exact_upload_and_preserves_source(self):
        self.client.get_object_tagging.return_value={"TagSet":[]}
        self.client.create_multipart_upload.return_value={"UploadId":"u"}
        self.client.upload_part_copy.return_value={"CopyPartResult":{"ETag":"part"}}
        active = Mock(side_effect=[None,RuntimeError("cancelled")])
        with self.assertRaisesRegex(RuntimeError,"cancelled"):
            s3_copy._copy_storage_class_version(self.client,bucket="b",object_key="k",source_version_id="source",
                target_class="DEEP_ARCHIVE",source_size=s3_copy.S3_SINGLE_COPY_MAX_BYTES+1,
                source_head={"Metadata":{"keep":"yes"}},intent_token="intent",check_active=active)
        self.client.abort_multipart_upload.assert_called_once_with(Bucket="b",Key="k",UploadId="u")
        self.client.delete_object.assert_not_called()
        self.assertEqual(self.client.create_multipart_upload.call_args.kwargs["ChecksumType"],"COMPOSITE")

    def test_nonconforming_composite_metadata_requires_full_exact_readback(self):
        self.client.head_object.return_value={"VersionId":"dest","ContentLength":3,"StorageClass":"STANDARD_IA",
                                              "ChecksumType":"COMPOSITE","ChecksumSHA256":"provider-incompatible"}
        self.client.get_object.return_value={'Body':io.BytesIO(b'abc')}
        s3_copy._verify_storage_class_destination(self.client,bucket="b",object_key="k",source_version_id="source",
            candidate_version_id="dest",target_class="STANDARD_IA",expected_size=3,
            expected_sha256_checksum=self.checksum,expected_composite_checksum="expected-2")
        self.assertEqual(self.client.get_object.call_args.kwargs['VersionId'],'dest')

    def test_wrong_composite_and_corrupt_bytes_fail_without_deletion(self):
        self.client.head_object.return_value={"VersionId":"dest","ContentLength":3,"StorageClass":"STANDARD_IA",
                                              "ChecksumType":"COMPOSITE","ChecksumSHA256":"provider-incompatible"}
        self.client.get_object.return_value={'Body':io.BytesIO(b'bad')}
        with self.assertRaisesRegex(RuntimeError,'integrity'):
            s3_copy._verify_storage_class_destination(self.client,bucket="b",object_key="k",source_version_id="source",
                candidate_version_id="dest",target_class="STANDARD_IA",expected_size=3,
                expected_sha256_checksum=self.checksum,expected_composite_checksum="expected-2")
        self.client.delete_object.assert_not_called()

    def test_cold_composite_destination_verifies_without_get_or_restore(self):
        self.client.head_object.return_value={"VersionId":"dest","ContentLength":3,"StorageClass":"DEEP_ARCHIVE",
                                              "ChecksumType":"COMPOSITE","ChecksumSHA256":"composite-2"}
        s3_copy._verify_storage_class_destination(self.client,bucket="b",object_key="k",source_version_id="source",
            candidate_version_id="dest",target_class="DEEP_ARCHIVE",expected_size=3,
            expected_sha256_checksum=self.checksum,expected_composite_checksum="composite-2")
        self.client.get_object.assert_not_called()
        self.client.restore_object.assert_not_called()
