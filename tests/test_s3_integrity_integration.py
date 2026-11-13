"""Live plaintext upload/verification/recovery proofs, run only in PR CI.

Uses the real SDK and an S3-compatible server with versioning and bucket BPA.
No mocking of the preflight or transfers. AWS CI buckets are pre-provisioned.
"""
from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from tests.catalog_support import fingerprint

from app.main import queue_jobs
from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.storage import process_jobs_once, s3_client
from app.services import vault_moves
from app.services.lifecycle_profiles import guided_profile, profile_to_json
from tests.s3_integration_support import (
    ensure_versioned_bucket, prepare_plain_vault, require_s3_env,
)


def _snapshot_mtime_ns(connection, version_id):
    """The mtime of the Upload Snapshot the Archive Version was uploaded from."""
    return connection.execute(
        """
        SELECT s.mtime_ns FROM archive_versions av
        JOIN upload_attempts a ON a.id = av.upload_attempt_id
        JOIN upload_snapshots s ON s.id = a.snapshot_id
        WHERE av.id = %s
        """,
        (version_id,),
    ).fetchone()["mtime_ns"]


@unittest.skipUnless(os.getenv("TEST_S3_ENDPOINT"), "Set TEST_S3_ENDPOINT for S3 integrity proofs")
class PlainS3IntegrityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env = require_s3_env()
        self.bucket = os.getenv("TEST_S3_BUCKET", "archive-ci")
        self.endpoint = os.environ["TEST_S3_ENDPOINT"]
        # Each test catalog owns a fresh Vault, so its namespace vaults/<uuid>
        # is unique; set by _vault() and cleaned in tearDown.
        self.prefix = None
        self._patch = patch.dict(os.environ, self.env, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        ensure_versioned_bucket(self.bucket)

    def tearDown(self):
        if self.prefix is None:
            return
        report = cleanup_prefix_versions(s3_client(), bucket=self.bucket, prefix=self.prefix)
        self.assertTrue(report.ok, "Test namespace cleanup failed")

    def _vault(self, root, relative_path, payload):
        source, database_path, self.prefix = prepare_plain_vault(
            root, relative_path=relative_path, payload=payload, bucket=self.bucket,
        )
        return source, database_path

    def _roundtrip(self, relative_path, payload, *, move=False, multipart_move=False, rename=False):
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, database_path = self._vault(root, relative_path, payload)
            local = source / relative_path
            original_mtime_ns = 1_597_614_120_123_456_789
            os.utime(local, ns=(original_mtime_ns, original_mtime_ns))
            with PostgresConnection(str(database_path)) as connection:
                connection.execute("UPDATE files SET local_mtime_ns=%s", (original_mtime_ns,))
            database_settings = SimpleNamespace(database_dsn=database_dsn(str(database_path)))
            worker_settings = SimpleNamespace(
                aws_region=self.env["AWS_DEFAULT_REGION"],
                operation_concurrency=1, restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings), patch("app.storage.settings", worker_settings):
                queue_jobs(relative_path, "upload", 2, 1)
                process_jobs_once()
                with PostgresConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    observed = catalog.get_file_by_path(2, relative_path)
                    job = connection.execute("SELECT status, message FROM jobs WHERE action='upload'").fetchone()
                    self.assertEqual(job["status"], "completed", job["message"])
                    version = observed["current_version"]
                    self.assertEqual(version["integrity"], "verified")
                    self.assertEqual(version["plaintext_sha256"], digest)
                    self.assertEqual(version["object_key"], f"{self.prefix}/{relative_path}")
                    head = s3_client().head_object(Bucket=self.bucket, Key=version["object_key"], VersionId=version["provider_version_id"])
                    self.assertEqual(head["VersionId"], version["provider_version_id"])
                    self.assertEqual(head["Metadata"]["frostvault-mtime-ns"], str(original_mtime_ns))
                    self.assertEqual(_snapshot_mtime_ns(connection, version["id"]), original_mtime_ns)
                if rename:
                    old_path = relative_path
                    relative_path = f"renamed/{old_path}"
                    renamed = source / relative_path
                    renamed.parent.mkdir(parents=True, exist_ok=True)
                    local.rename(renamed)
                    local = renamed
                    with PostgresConnection(str(database_path)) as connection:
                        catalog = ArchiveCatalog(connection)
                        timestamp = "2026-11-11T12:00:00+00:00"
                        catalog.mark_local_copy_missing(observed["id"], observed_at=timestamp)
                        catalog.observe_local_copy(vault_id=2, path=relative_path, file_type="regular",
                            size=len(payload), mtime_ns=original_mtime_ns, observed_at=timestamp)
                        fingerprint(catalog, 2, relative_path, digest)
                        catalog.confirm_file_rename(vault_file_id=observed["id"], new_path=relative_path,
                            changed_at=timestamp, vault_id=2)
                    queue_jobs(relative_path, "rename", 2, 1)
                    process_jobs_once()
                    with PostgresConnection(str(database_path)) as connection:
                        job = connection.execute("SELECT status,message FROM jobs WHERE action='rename'").fetchone()
                        self.assertEqual(job["status"], "completed", job["message"])
                        renamed_version = ArchiveCatalog(connection).get_file_by_path(2, relative_path)["current_version"]
                        head = s3_client().head_object(Bucket=self.bucket, Key=renamed_version["object_key"], VersionId=renamed_version["provider_version_id"])
                        # A server-side copy keeps the exact metadata (MetadataDirective=COPY).
                        self.assertEqual(head["Metadata"]["frostvault-mtime-ns"], str(original_mtime_ns))
                if move:
                    from contextlib import ExitStack
                    with PostgresConnection(str(database_path)) as connection:
                        connection.execute("UPDATE vaults SET storage_plan=%s::jsonb WHERE id=2",
                            (profile_to_json(guided_profile('economical')),))
                        scheduled = vault_moves.schedule(connection,vault_id=2,target='STANDARD_IA',
                            user_id=1,acknowledged=True,timestamp='2026-11-05T12:00:00+00:00')
                    with ExitStack() as stack:
                        if multipart_move:
                            # Real multipart provider calls with CI-sized data;
                            # production still has the actual 5 GiB cutoff.
                            for name in ('app.services.s3_copy.S3_SINGLE_COPY_MAX_BYTES',
                                         'app.services.vault_moves.S3_SINGLE_COPY_MAX_BYTES'):
                                stack.enter_context(patch(name,8*1024*1024))
                            stack.enter_context(patch('app.services.s3_copy.S3_MULTIPART_COPY_PART_BYTES',5*1024*1024))
                        process_jobs_once()
                    with PostgresConnection(str(database_path)) as connection:
                        moved=connection.execute('SELECT status,message,archive_version_id FROM jobs WHERE id=%s',
                                                 (scheduled['job_ids'][0],)).fetchone()
                        self.assertEqual(moved['status'],'completed',moved['message'])
                        self.assertNotEqual(moved['archive_version_id'],version['id'])
                        # The superseded source is deleted in the cloud and then
                        # leaves the catalog (Current Snapshot, ADR-0035).
                        old=connection.execute('SELECT 1 FROM archive_versions WHERE id=%s',
                                               (version['id'],)).fetchone()
                        self.assertIsNone(old)
                        listed=s3_client().list_object_versions(Bucket=self.bucket,Prefix=version['object_key'])
                        self.assertEqual(len(listed.get('Versions',[])),1)
                        self.assertNotEqual(listed['Versions'][0]['VersionId'],version['provider_version_id'])
                with PostgresConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    local.unlink()
                    catalog.mark_local_copy_missing(observed["id"], observed_at="2026-07-22T12:30:00+00:00")
                queue_jobs(relative_path, "recover", 2, 1)
                process_jobs_once()
                with PostgresConnection(str(database_path)) as connection:
                    job = connection.execute("SELECT status, message FROM jobs WHERE action='recover'").fetchone()
                    self.assertEqual(job["status"], "completed", job["message"])
                self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), digest)
                self.assertEqual(local.stat().st_mtime_ns, original_mtime_ns)
                with PostgresConnection(str(database_path)) as connection:
                    recovered = connection.execute("SELECT local_mtime_ns,local_recovered_at FROM files WHERE id=%s", (observed["id"],)).fetchone()
                    self.assertEqual(recovered["local_mtime_ns"], original_mtime_ns)
                    self.assertIsNotNone(recovered["local_recovered_at"])
                    # Even a catalogless recovery has exact metadata after a
                    # single/multipart storage-class copy (MetadataDirective).
                    latest = ArchiveCatalog(connection).get_file_by_path(2, relative_path)["current_version"]
                    head = s3_client().head_object(Bucket=self.bucket, Key=latest["object_key"], VersionId=latest["provider_version_id"])
                    self.assertEqual(head["Metadata"]["frostvault-mtime-ns"], str(original_mtime_ns))

    def test_raw_receipt_after_real_sdk_upload_survives_post_response_failure_without_extra_version(self):
        from app.services import upload_provenance
        with tempfile.TemporaryDirectory() as directory:
            payload = b"real compatible-provider raw receipt gap"
            source, database_path = self._vault(Path(directory), "receipt/gap.bin", payload)
            with patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(str(database_path)))):
                queue_jobs("receipt/gap.bin", "upload", 2, 1)
                from app import storage
                real_send = storage.upload_s3_file

                def send_then_fail(*args, **kwargs):
                    # The real SDK PUT lands and its raw receipt is journaled
                    # (on_acknowledged); the worker then fails before publishing.
                    real_send(*args, **kwargs)
                    raise RuntimeError("injected post-response failure")

                with patch.object(storage, "upload_s3_file", side_effect=send_then_fail):
                    process_jobs_once()
                with PostgresConnection(str(database_path)) as connection:
                    original = connection.execute("SELECT * FROM jobs").fetchone()
                    self.assertEqual(original["status"],"failed")
                    self.assertIsNone(original["archive_version_id"])
                    intent = upload_provenance.load_for_job(connection, original["id"])
                    receipt = upload_provenance.receipt(connection,intent.id)
                    vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
                    resumed = upload_provenance.schedule_resume(connection,vault=vault,path="receipt/gap.bin",user_id=1,timestamp="2026-11-06T00:00:00+00:00")
                before = s3_client().list_object_versions(Bucket=self.bucket,Prefix=f"{self.prefix}/receipt/gap.bin")
                self.assertEqual([v["VersionId"] for v in before["Versions"]],[receipt["VersionId"]])
                process_jobs_once()
                with PostgresConnection(str(database_path)) as connection:
                    final = connection.execute("SELECT status,archive_version_id FROM jobs WHERE id=%s", (resumed["job_id"],)).fetchone()
                    self.assertEqual(final["status"],"completed")
                    keeper = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (final["archive_version_id"],)).fetchone()
                    self.assertEqual(keeper["provider_version_id"],receipt["VersionId"])
                    self.assertEqual(bytes(keeper["plaintext_sha256"]),hashlib.sha256(payload).digest())
                    self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=%s", (original["id"],)).fetchone()["status"],"failed")
                after = s3_client().list_object_versions(Bucket=self.bucket,Prefix=f"{self.prefix}/receipt/gap.bin")
                self.assertEqual([v["VersionId"] for v in after["Versions"]],[receipt["VersionId"]],"Recovery issued a second provider write")

    def test_interrupted_multipart_is_aborted_by_its_journaled_upload_id_and_the_upload_completes(self):
        # #491: a crashed multipart upload whose abort never reached S3 blocks the proof of
        # absence; resuming aborts exactly the journaled UploadId, then sends once more.
        from botocore.exceptions import ClientError
        from app.services import upload_provenance
        from app.services.s3_upload import _UploadClient
        relative_path, payload = "multipart/crashed.bin", b"c" * (9 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            source, database_path = self._vault(Path(directory), relative_path, payload)
            key = f"{self.prefix}/{relative_path}"
            with patch("app.database.settings", SimpleNamespace(database_dsn=database_dsn(str(database_path)))):
                queue_jobs(relative_path, "upload", 2, 1)
                lost = ClientError({"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "AbortMultipartUpload")
                with patch.object(_UploadClient, "upload_part", side_effect=RuntimeError("injected crash after CreateMultipartUpload")), \
                        patch.object(_UploadClient, "abort_multipart_upload", side_effect=lost):
                    process_jobs_once()
                client = s3_client()
                left = [u for u in client.list_multipart_uploads(Bucket=self.bucket, Prefix=key).get("Uploads", []) if u["Key"] == key]
                self.assertEqual(len(left), 1, "the crash must leave the incomplete multipart upload behind")
                with PostgresConnection(str(database_path)) as connection:
                    original = connection.execute("SELECT * FROM jobs").fetchone()
                    self.assertEqual(original["status"], "failed")
                    intent = upload_provenance.load_for_job(connection, original["id"])
                    journal = connection.execute("SELECT upload_id,closed_at FROM upload_multiparts WHERE attempt_id=%s", (intent.id,)).fetchall()
                    self.assertEqual([(r["upload_id"], r["closed_at"]) for r in journal], [(left[0]["UploadId"], None)])
                    vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
                    resumed = upload_provenance.schedule_resume(connection, vault=vault, path=relative_path, user_id=1, timestamp="2026-11-07T00:00:00+00:00")
                process_jobs_once()
                with PostgresConnection(str(database_path)) as connection:
                    final = connection.execute("SELECT status,message,archive_version_id FROM jobs WHERE id=%s", (resumed["job_id"],)).fetchone()
                    self.assertEqual(final["status"], "completed", final["message"])
                    closed = [r["closed_at"] is not None for r in connection.execute("SELECT closed_at FROM upload_multiparts WHERE upload_id=%s", (left[0]["UploadId"],)).fetchall()]
                    self.assertEqual(closed, [True])
                listed = client.list_object_versions(Bucket=self.bucket, Prefix=key)
                self.assertEqual(len(listed.get("Versions", [])), 1)
                self.assertEqual([u for u in client.list_multipart_uploads(Bucket=self.bucket, Prefix=key).get("Uploads", []) if u["Key"] == key], [])

    def test_rename_preserves_timestamp_in_s3_catalog_and_recovery(self):
        self._roundtrip("photos/original.jpg", b"original photo", rename=True)

    def test_plain_upload_and_recovery_match_sha256(self):
        self._roundtrip("docs/report.txt", b"live-integrity-payload")

    def test_empty_file_upload_and_recovery(self):
        self._roundtrip("empty/zero.bin", b"")

    def test_unicode_path_upload_and_recovery(self):
        self._roundtrip("café/文档.txt", "unicodé-bytes".encode())

    def test_multipart_cutoff_upload_and_recovery(self):
        self._roundtrip("multipart/blob.bin", b"m" * (9 * 1024 * 1024))

    def test_vault_wide_copy_verified_replacement_and_recovery(self):
        self._roundtrip('moves/photo.bin',b'copy-integrity-payload',move=True)

    def test_multipart_vault_move_verified_replacement_and_recovery(self):
        self._roundtrip('moves/multipart.bin',b'm'*(9*1024*1024),move=True,multipart_move=True)
