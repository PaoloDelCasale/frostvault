"""PostgreSQL worker acceptance for #460; disposable PR CI only."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

from app.catalog import ArchiveCatalog
from app.main import queue_jobs
from app.services.operation_policies import OperationPolicy, get_policy, set_policy
from app.services.upload_snapshot import sha256_b64
from app.storage import process_jobs_once, expected_cloud_key
from tests.postgres import PostgresConnection, database_dsn
from tests.snapshot_s3_stub import VersionedS3Stub
from tests.test_upload_verification import _prepare_plain_vault


class UploadModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.payload = b"independent snapshot A"
        self.source, self.identity = _prepare_plain_vault(Path(temp.name), relative_path="a.txt", payload=self.payload)
        self.patch = patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(str(self.identity))))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.puts = 0
        self.upload_response = {"VersionId": "raw-version", "ChecksumSHA256": sha256_b64(self.payload)}
        self.head_error = None
        self.head_before_read = {}
        self.head_after_read = {}
        self.verification_failures = []
        self.proof = {"VersionId": "raw-version", "ContentLength": len(self.payload), "ChecksumType": "FULL_OBJECT", "ChecksumSHA256": sha256_b64(self.payload)}
        self.on_upload = None
        self.s3 = VersionedS3Stub(version_bytes=self.payload, head_object=self.head)

    def head(self, **kwargs):
        if self.head_error:
            raise self.head_error
        if kwargs.get("ChecksumMode"):
            if isinstance(self.proof, Exception): raise self.proof
            return self.proof
        extra = self.head_after_read if self.s3.reads else self.head_before_read
        return {"VersionId": kwargs.get("VersionId") or "raw-version", "ContentLength": len(self.payload), "StorageClass": "STANDARD", "ETag": "etag", **extra}

    def upload(self, job, *, source, key):
        self.puts += 1
        snapshot = job["_upload_snapshot"]
        self.assertEqual(snapshot.plaintext_sha256, hashlib.sha256(self.payload).hexdigest())
        # Durable Job evidence exists BEFORE any provider write.
        with PostgresConnection(str(self.identity)) as connection:
            before = connection.execute("SELECT upload_snapshot_json FROM jobs WHERE id=%s", (job["id"],)).fetchone()
        self.assertEqual(before["upload_snapshot_json"], snapshot.as_json())
        self.s3.versions.setdefault(key, set()).add(self.upload_response["VersionId"])
        job["_on_upload_acknowledged"](self.upload_response)
        if self.on_upload: self.on_upload()
        return self.upload_response

    def queue(self):
        queue_jobs("a.txt", "upload", 2, 1)

    def run_worker(self):
        with patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client", return_value=self.s3), patch("app.storage.upload_s3_file", side_effect=self.upload), patch("app.storage._record_verification_failure", side_effect=self.verification_failures.append):
            process_jobs_once()

    def rows(self):
        with PostgresConnection(str(self.identity)) as connection:
            job = connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            versions = connection.execute("SELECT * FROM archive_versions ORDER BY version_number").fetchall()
            item = ArchiveCatalog(connection).get_file_by_path(2, "a.txt")
        return job, versions, item

    def set_mode(self, mode):
        with PostgresConnection(str(self.identity)) as connection:
            set_policy(connection, 2, OperationPolicy(upload_verification=mode))

    def test_inherited_default_s3_proof_verifies_without_GET_and_allows_usual_guards(self):
        with PostgresConnection(str(self.identity)) as connection:
            self.assertIsNone(get_policy(connection, 2).upload_verification)
            self.assertEqual(get_policy(connection, 2).effective_upload_verification(), "s3_checksum")
        self.queue(); self.run_worker()
        job, versions, item = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_method"], "s3_checksum")
        self.assertEqual(versions[0]["verification_egress_bytes"], 0)
        self.assertEqual(versions[0]["integrity"], "verified")
        self.assertEqual(item["local_copy"]["plaintext_sha256"], item["latest_version"]["plaintext_sha256"])
        self.assertEqual(self.s3.reads, [])
        with PostgresConnection(str(self.identity)) as connection:
            self.assertTrue(ArchiveCatalog(connection).list_file_rows(2)[0]["cleanup_eligible"])

    def test_requested_mode_frozen_at_admission_and_history_not_rewritten(self):
        self.set_mode("read_back"); self.queue(); self.set_mode("s3_checksum")
        self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["upload_verification_requested"], "read_back")
        self.assertEqual(versions[0]["verification_method"], "read_back")
        self.assertEqual(versions[0]["verification_egress_bytes"], len(self.payload))
        self.assertEqual(len(self.s3.reads), 1)
        self.set_mode(None)
        self.assertEqual(self.rows()[1][0]["verification_method"], "read_back")

    def test_valid_ordinary_HEAD_proof_is_reused_with_its_actual_source(self):
        import json
        self.upload_response.pop("ChecksumSHA256")
        self.head_before_read = {"ChecksumType": "FULL_OBJECT", "ChecksumSHA256": sha256_b64(self.payload)}
        # A redundant checksum-enabled HEAD would fail: the ordinary exact HEAD
        # has already supplied independently validated cryptographic evidence.
        self.proof = AssertionError("redundant checksum HEAD")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_method"], "s3_checksum")
        self.assertEqual(json.loads(versions[0]["provider_checksums"])["source"], "ordinary_head")
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.puts, 1)

    def test_ordinary_HEAD_proof_never_overrides_an_explicit_read_back(self):
        self.set_mode("read_back")
        self.upload_response.pop("ChecksumSHA256")
        self.head_before_read = {"ChecksumType": "FULL_OBJECT", "ChecksumSHA256": sha256_b64(self.payload)}
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_method"], "read_back")
        self.assertEqual(len(self.s3.reads), 1)

    def test_checksum_missing_falls_back_visibly_exact_GET(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = {"VersionId": "raw-version", "ContentLength": len(self.payload)}
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_method"], "read_back")
        self.assertEqual(versions[0]["verification_fallback_reason"], "checksum_missing")
        self.assertEqual(self.s3.reads[-1]["VersionId"], "raw-version")
        self.assertEqual(versions[0]["verification_egress_bytes"], len(self.payload))

    def test_unsupported_checksum_HEAD_falls_back_not_auth_or_network(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = ClientError({"Error": {"Code": "NotImplemented"}}, "HeadObject")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_fallback_reason"], "checksum_api_unsupported")
        self.assertEqual(self.s3.reads[0]["VersionId"], "raw-version")

    def test_checksum_HEAD_auth_error_preserves_ack_without_fallback(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job['retry_count'],0)
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "unverified")
        self.assertEqual(self.s3.reads, [])
        # Auth recovery resumes the frozen exact version, not fallback or PUT.
        from app.services import cloud_preflight_gate
        self.proof = {'VersionId':'raw-version','ContentLength':len(self.payload),
            'ChecksumType':'FULL_OBJECT','ChecksumSHA256':sha256_b64(self.payload)}
        cloud_preflight_gate.check(lambda:PostgresConnection(str(self.identity)),lambda:None)
        self.run_worker()
        self.assertEqual(self.rows()[0]['status'],'completed')
        self.assertEqual(self.puts,1)
        self.assertEqual(self.s3.reads,[])

    def test_generic_invalid_checksum_HEAD_request_is_not_a_fallback(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = ClientError({"Error": {"Code": "InvalidRequest"}}, "HeadObject")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "unverified")
        self.assertIsNone(versions[0]["verification_fallback_reason"])
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.deleted, [])

    def test_unrelated_invalid_checksum_HEAD_argument_is_not_a_fallback(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = ClientError({"Error": {"Code": "InvalidArgument", "ArgumentName": "VersionId"}}, "HeadObject")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "unverified")
        self.assertIsNone(versions[0]["verification_fallback_reason"])
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.deleted, [])

    def test_explicit_unsupported_checksum_mode_argument_can_fall_back(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof = ClientError({"Error": {"Code": "InvalidArgument", "ArgumentName": "ChecksumMode"}}, "HeadObject")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["verification_fallback_reason"], "checksum_api_unsupported")
        self.assertEqual(self.s3.reads[0]["VersionId"], "raw-version")

    def test_wrong_completion_checksum_retains_version_and_blocks_no_fallback(self):
        self.upload_response["ChecksumSHA256"] = "wrong"
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(job["archive_version_id"], versions[0]["id"])
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(self.verification_failures, ["mismatch"])

    def test_wrong_checksum_from_HEAD_retains_version_and_blocks(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof["ChecksumSHA256"] = "wrong"
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.verification_failures, ["mismatch"])

    def test_read_back_request_does_not_silently_downgrade_contradictory_checksum(self):
        self.set_mode("read_back")
        self.upload_response["ChecksumSHA256"] = "wrong"
        self.queue(); self.run_worker()
        self.assertEqual(self.rows()[1][0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])

    def test_contradictory_ordinary_HEAD_blocks_and_counts_once_before_read(self):
        self.head_before_read = {"ChecksumSHA256": "wrong"}
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(self.verification_failures, ["mismatch"])

    def assert_final_HEAD_contradiction_blocks_cleanup(self, fields):
        self.set_mode("read_back")
        self.head_after_read = fields
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(len(self.s3.reads), 1)
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(self.verification_failures, ["mismatch"])
        self.assertEqual(versions[0]["verification_egress_bytes"], len(self.payload))

    def test_wrong_checksum_on_post_read_HEAD_never_verifies_or_cleans(self):
        self.assert_final_HEAD_contradiction_blocks_cleanup({"ChecksumSHA256": "wrong"})

    def test_wrong_type_on_post_read_HEAD_never_verifies_or_cleans(self):
        self.assert_final_HEAD_contradiction_blocks_cleanup({"ChecksumType": "COMPOSITE"})

    def assert_final_HEAD_identity_or_size_blocks_cleanup(self, fields):
        self.set_mode("read_back")
        self.head_after_read = fields
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["integrity"], "unverified")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(len(self.s3.reads), 1)
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(versions[0]["verification_egress_bytes"], len(self.payload))

    def test_wrong_identity_on_post_read_HEAD_never_verifies_or_cleans(self):
        self.assert_final_HEAD_identity_or_size_blocks_cleanup({"VersionId": "other-version"})

    def test_wrong_size_on_post_read_HEAD_never_verifies_or_cleans(self):
        self.assert_final_HEAD_identity_or_size_blocks_cleanup({"ContentLength": len(self.payload) + 1})

    def test_multipart_composite_proof_verifies_not_plaintext_digest(self):
        from app.services.upload_snapshot import prepare_upload_snapshot, MULTIPART_THRESHOLD
        self.payload = b"a" * MULTIPART_THRESHOLD + b"final-part"
        path = self.source / "a.txt"
        path.write_bytes(self.payload)
        snapshot, _ = prepare_upload_snapshot(path, lambda: None)
        with PostgresConnection(str(self.identity)) as connection:
            ArchiveCatalog(connection).observe_local_copy(vault_id=2, path="a.txt", file_type="regular", size=len(self.payload), mtime_ns=path.stat().st_mtime_ns, observed_at="2026-11-06")
        self.upload_response = {"VersionId": "raw-version", "ChecksumType": "COMPOSITE", "ChecksumSHA256": snapshot.checksum_sha256}
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed", job["message"])
        self.assertEqual(versions[0]["verification_method"], "s3_checksum")
        self.assertEqual(versions[0]["plaintext_sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(versions[0]["upload_snapshot_json"], snapshot.as_json())

    def test_wrong_checksum_type_from_HEAD_retains_version_and_blocks(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof["ChecksumType"] = "COMPOSITE"
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])

    def test_wrong_checksum_identity_from_HEAD_retains_raw_version_and_blocks(self):
        self.upload_response.pop("ChecksumSHA256")
        self.proof["VersionId"] = "different-current"
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(versions[0]["integrity"], "mismatch")
        self.assertEqual(self.s3.reads, [])

    def test_HEAD_failure_after_raw_ack_retry_uses_same_version_no_new_PUT(self):
        self.head_error = RuntimeError("connection reset by peer")
        self.queue(); self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "retrying")
        self.assertEqual(versions[0]["provider_version_id"], "raw-version")
        self.assertEqual(self.puts, 1)
        self.head_error = None
        self.set_mode("read_back")  # Retry still uses original S3 mode.
        with PostgresConnection(str(self.identity)) as connection:
            connection.execute("UPDATE jobs SET retry_after='2020-01-01T00:00:00+00:00'")
        self.run_worker()
        self.assertEqual(self.rows()[0]["status"], "completed")
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.s3.reads, [])

    def test_verified_A_never_protects_B_even_when_same_size(self):
        self.on_upload = lambda: (self.source / "a.txt").write_bytes(b"independent snapshot B")
        self.queue(); self.run_worker()
        job, versions, item = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(versions[0]["integrity"], "verified")
        self.assertEqual(item["local_copy"]["plaintext_sha256"], hashlib.sha256(b"independent snapshot B").hexdigest())
        with PostgresConnection(str(self.identity)) as connection:
            listing = ArchiveCatalog(connection).list_file_rows(2)[0]
        self.assertFalse(listing["cleanup_eligible"])
        self.assertNotEqual(listing["state"], "protected")
        self.assertEqual(self.s3.reads, [])

    def test_verified_proof_is_not_completed_until_guarded_cleanup_succeeds(self):
        self.queue()
        with patch("app.storage.purge_current_snapshot_superseded", side_effect=RuntimeError("Snapshot found uncatalogued cloud content; no deletion allowed")):
            self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(versions[0]["integrity"], "verified")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.s3.deleted, [])

    def prepare_verified_cleanup_retry(self, *, unlink=False):
        with PostgresConnection(str(self.identity)) as connection:
            vault = connection.execute("SELECT s3_prefix FROM vaults WHERE id=2").fetchone()
            key = expected_cloud_key("a.txt", vault["s3_prefix"])
            catalog = ArchiveCatalog(connection)
            old = catalog.record_archive_version(
                vault_id=2, path="a.txt", object_key=key, provider_version_id="prior-snapshot",
                size=len(b"prior content"), storage_class="STANDARD", etag="old",
                uploaded_at="2026-11-01T00:00:00+00:00", observed_at="2026-11-01T00:00:00+00:00",
                scan_id="prior", origin="upload",
            )
            catalog.mark_version_verified(old, plaintext_sha256=hashlib.sha256(b"prior content").hexdigest(),
                                          verified_at="2026-11-01T00:00:00+00:00")
            owner = connection.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (old,)).fetchone()["vault_file_id"]
            connection.execute("INSERT INTO jobs(vault_id,vault_file_id,archive_version_id,path,action,status,requested_at,updated_at,upload_plaintext_sha256) VALUES (2,%s,%s,'a.txt','upload','completed','2026-11-01','2026-11-01',%s)", (owner, old, hashlib.sha256(b"prior content").hexdigest()))
        self.s3.versions.setdefault(key, set()).add("prior-snapshot")
        self.queue()
        with patch("app.storage.purge_current_snapshot_superseded", side_effect=RuntimeError("connection reset by peer")):
            self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "retrying")
        self.assertEqual(versions[-1]["integrity"], "verified")
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.s3.deleted, [])
        with PostgresConnection(str(self.identity)) as connection:
            connection.execute("UPDATE jobs SET retry_after='2020-01-01T00:00:00+00:00'")
            if unlink:
                connection.execute("UPDATE jobs SET archive_version_id=NULL WHERE id=%s", (job["id"],))
        return key

    def assert_verified_retry_contradiction_preserves_both_copies(self, fields, *, unlink=False):
        key = self.prepare_verified_cleanup_retry(unlink=unlink)
        self.head_before_read = fields
        self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(versions[-1]["integrity"], "mismatch")
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.deleted, [])
        self.assertEqual(self.s3.versions[key], {"prior-snapshot", "raw-version"})
        self.assertEqual(self.verification_failures[-1], "mismatch")

    def test_verified_linked_cleanup_retry_blocks_contradictory_checksum(self):
        self.assert_verified_retry_contradiction_preserves_both_copies({"ChecksumSHA256": "wrong"})

    def test_verified_linked_cleanup_retry_blocks_contradictory_type(self):
        self.assert_verified_retry_contradiction_preserves_both_copies({"ChecksumType": "COMPOSITE"})

    def test_verified_reusable_cleanup_retry_blocks_contradictory_checksum(self):
        self.assert_verified_retry_contradiction_preserves_both_copies({"ChecksumSHA256": "wrong"}, unlink=True)

    def test_verified_cleanup_retry_missing_checksum_needs_no_GET_or_PUT(self):
        key = self.prepare_verified_cleanup_retry()
        self.run_worker()
        self.assertEqual(self.rows()[0]["status"], "completed")
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.s3.reads, [])
        self.assertEqual(self.s3.versions[key], {"raw-version"})
        self.assertEqual(len(self.s3.deleted), 1)

    def test_legacy_expected_missing_requires_durable_plaintext_GET_not_local_B(self):
        self.set_mode("read_back"); self.queue()
        self.head_error = RuntimeError("connection reset by peer")
        self.run_worker()
        with PostgresConnection(str(self.identity)) as connection:
            connection.execute("UPDATE archive_versions SET upload_snapshot_json=NULL, upload_verification_requested=NULL")
            connection.execute("UPDATE jobs SET upload_snapshot_json=NULL, upload_verification_requested=NULL, retry_after='2020-01-01T00:00:00+00:00'")
            # This fixture represents a pre-0063/pre-0064 upload, not deliberate
            # tampering with the immutable baseline of a new journalled intent.
            connection.execute("DELETE FROM upload_receipts")
            connection.execute("DELETE FROM upload_intent_jobs")
            connection.execute("DELETE FROM upload_intents")
            connection.execute("DELETE FROM snapshot_cleanup_inventories")
        (self.source / "a.txt").write_bytes(b"independent snapshot B")
        self.head_error = None
        self.run_worker()
        job, versions, _ = self.rows()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.puts, 1)
        self.assertEqual(versions[0]["verification_method"], "read_back")
        self.assertEqual(versions[0]["verification_fallback_reason"], "legacy_expected_missing")
        self.assertEqual(versions[0]["plaintext_sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.s3.reads[0]["VersionId"], "raw-version")
