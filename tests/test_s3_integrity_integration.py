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
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog

from app.main import queue_jobs
from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.storage import process_jobs_once, s3_client
from app.services import vault_moves
from app.services.lifecycle_profiles import guided_profile, profile_to_json
from tests.s3_integration_support import (
    ensure_versioned_bucket, namespaced_prefix, prepare_plain_vault,
    require_s3_env,
)


@unittest.skipUnless(os.getenv("TEST_S3_ENDPOINT"), "Set TEST_S3_ENDPOINT for S3 integrity proofs")
class PlainS3IntegrityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env = require_s3_env()
        self.bucket = os.getenv("TEST_S3_BUCKET", "archive-ci")
        self.endpoint = os.environ["TEST_S3_ENDPOINT"]
        self.prefix = namespaced_prefix(f"ci-runs/{uuid.uuid4().hex}")
        self._patch = patch.dict(os.environ, self.env, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        ensure_versioned_bucket(self.bucket)

    def tearDown(self):
        report = cleanup_prefix_versions(s3_client(), bucket=self.bucket, prefix=self.prefix)
        self.assertTrue(report.ok, "Test namespace cleanup failed")

    def _roundtrip(self, relative_path, payload, *, move=False, multipart_move=False):
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, database_path = prepare_plain_vault(
                root, relative_path=relative_path, payload=payload, bucket=self.bucket,
                prefix=self.prefix,
            )
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
                    version = observed["latest_version"]
                    self.assertEqual(version["integrity"], "verified")
                    self.assertEqual(version["plaintext_sha256"], digest)
                    self.assertEqual(version["object_key"], f"{self.prefix}/{relative_path}")
                    head = s3_client().head_object(Bucket=self.bucket, Key=version["object_key"], VersionId=version["provider_version_id"])
                    self.assertEqual(head["VersionId"], version["provider_version_id"])
                    local = source / relative_path
                if move:
                    from contextlib import ExitStack
                    with PostgresConnection(str(database_path)) as connection:
                        connection.execute("UPDATE vaults SET storage_plan_json=%s WHERE id=2",
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
                        old=connection.execute('SELECT availability,provider_version_id FROM archive_versions WHERE id=%s',
                                               (version['id'],)).fetchone()
                        self.assertEqual(old['availability'],'purged')
                        listed=s3_client().list_object_versions(Bucket=self.bucket,Prefix=version['object_key'])
                        self.assertEqual(len(listed.get('Versions',[])),1)
                        self.assertNotEqual(listed['Versions'][0]['VersionId'],old['provider_version_id'])
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
