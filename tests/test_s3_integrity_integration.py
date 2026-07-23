"""Live S3-compatible integrity proofs (issue #13).

Seams under test:
- ``queue_jobs`` + ``process_jobs_once`` / ``process_upload`` / ``process_recover``
  against a real S3-compatible endpoint (MinIO in CI) and real Rclone.
- ``cleanup_prefix_versions`` guarantees the test prefix is empty afterward.

Skipped unless ``TEST_S3_ENDPOINT`` is set so pull-request CI without MinIO
stays deterministic and credential-free. The MinIO service job sets the env.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.services.s3_prefix_cleanup import cleanup_prefix_versions
from app.storage import process_jobs_once, s3_client
from tests.s3_integration_support import (
    ensure_versioned_bucket,
    namespaced_prefix,
    new_master_key,
    prepare_crypt_vault,
    prepare_plain_vault,
    require_s3_env,
    write_plain_rclone_config,
    write_s3_rclone_config,
)


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@unittest.skipUnless(
    os.getenv("TEST_S3_ENDPOINT"),
    "Set TEST_S3_ENDPOINT for S3-compatible integrity integration tests",
)
class PlainS3IntegrityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = require_s3_env()
        self.bucket = os.environ.get("TEST_S3_BUCKET", "archive-ci")
        self.endpoint = os.environ["TEST_S3_ENDPOINT"]
        self.prefix = namespaced_prefix(f"ci-runs/{uuid.uuid4().hex}")
        self._patch = patch.dict(os.environ, self.env, clear=False)
        self._patch.start()
        ensure_versioned_bucket(self.bucket)

    def tearDown(self) -> None:
        try:
            report = cleanup_prefix_versions(
                s3_client(), bucket=self.bucket, prefix=self.prefix
            )
            if not report.ok:
                raise AssertionError(
                    f"Cleanup left leftovers: {report.leftover_keys} ({report.message})"
                )
        finally:
            self._patch.stop()

    def _worker_settings(self, rclone_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            aws_region=self.env["AWS_DEFAULT_REGION"],
            rclone_config=str(rclone_path),
            operation_concurrency=1,
            restore_poll_interval=900,
            archive_master_key="",
        )

    def _run_plain_upload_and_assert(
        self, *, relative_path: str, payload: bytes
    ) -> None:
        expected = _sha256_hex(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rclone_path = root / "rclone.conf"
            remote = write_plain_rclone_config(
                rclone_path,
                endpoint=self.endpoint,
                bucket=self.bucket,
                prefix=self.prefix,
                access_key=self.env["AWS_ACCESS_KEY_ID"],
                secret_key=self.env["AWS_SECRET_ACCESS_KEY"],
            )
            _source, database_path = prepare_plain_vault(
                root,
                relative_path=relative_path,
                payload=payload,
                bucket=self.bucket,
                prefix=self.prefix,
                rclone_remote=remote,
            )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = self._worker_settings(rclone_path)

            with patch("app.database.settings", database_settings):
                queue_jobs(relative_path, "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.services.rclone_runtime.settings", worker_settings),
                ):
                    process_jobs_once()

                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    observed = catalog.get_file_by_path(2, relative_path)
                    job = connection.execute(
                        "SELECT status, message FROM jobs WHERE path=%s",
                        (relative_path,),
                    ).fetchone()
                    version = observed["latest_version"]

                self.assertEqual(job["status"], "completed", job["message"])
                self.assertEqual(version["integrity"], "verified")
                self.assertEqual(version["plaintext_sha256"], expected)

                local = root / "source" / relative_path
                local.unlink()
                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    file_row = catalog.get_file_by_path(2, relative_path)
                    catalog.mark_local_copy_missing(
                        file_row["id"],
                        observed_at="2026-07-22T12:30:00+00:00",
                    )

                queue_jobs(relative_path, "recover", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.services.rclone_runtime.settings", worker_settings),
                ):
                    process_jobs_once()

                with SQLiteConnection(str(database_path)) as connection:
                    recover_job = connection.execute(
                        """
                        SELECT status, message FROM jobs
                        WHERE path=%s AND action='recover'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (relative_path,),
                    ).fetchone()

                self.assertEqual(
                    recover_job["status"], "completed", recover_job["message"]
                )
                self.assertTrue(local.is_file())
                self.assertEqual(_sha256_hex(local.read_bytes()), expected)

    def test_plain_upload_and_recovery_match_sha256(self) -> None:
        """Upload then recover against live object storage; digests must match."""
        self._run_plain_upload_and_assert(
            relative_path="docs/report.txt",
            payload=b"live-integrity-payload",
        )

    def test_empty_file_upload_and_recovery(self) -> None:
        self._run_plain_upload_and_assert(
            relative_path="empty/zero.bin",
            payload=b"",
        )

    def test_unicode_path_upload_and_recovery(self) -> None:
        self._run_plain_upload_and_assert(
            relative_path="café/文档.txt",
            payload="unicodé-bytes".encode("utf-8"),
        )

    def test_multipart_cutoff_upload_and_recovery(self) -> None:
        """Force multipart via a tiny upload_cutoff and still verify digests."""
        payload = b"m" * 4096
        expected = _sha256_hex(payload)
        relative_path = "multipart/blob.bin"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rclone_path = root / "rclone.conf"
            remote = write_plain_rclone_config(
                rclone_path,
                endpoint=self.endpoint,
                bucket=self.bucket,
                prefix=self.prefix,
                access_key=self.env["AWS_ACCESS_KEY_ID"],
                secret_key=self.env["AWS_SECRET_ACCESS_KEY"],
                upload_cutoff="1Ki",
            )
            _source, database_path = prepare_plain_vault(
                root,
                relative_path=relative_path,
                payload=payload,
                bucket=self.bucket,
                prefix=self.prefix,
                rclone_remote=remote,
            )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = self._worker_settings(rclone_path)
            with patch("app.database.settings", database_settings):
                queue_jobs(relative_path, "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.services.rclone_runtime.settings", worker_settings),
                ):
                    process_jobs_once()
                with SQLiteConnection(str(database_path)) as connection:
                    version = ArchiveCatalog(connection).get_file_by_path(
                        2, relative_path
                    )["latest_version"]
                    job = connection.execute(
                        "SELECT status, message FROM jobs WHERE path=%s",
                        (relative_path,),
                    ).fetchone()
                self.assertEqual(job["status"], "completed", job["message"])
                self.assertEqual(version["integrity"], "verified")
                self.assertEqual(version["plaintext_sha256"], expected)


@unittest.skipUnless(
    os.getenv("TEST_S3_ENDPOINT"),
    "Set TEST_S3_ENDPOINT for S3-compatible integrity integration tests",
)
class CryptS3IntegrityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = require_s3_env()
        self.bucket = os.environ.get("TEST_S3_BUCKET", "archive-ci")
        self.endpoint = os.environ["TEST_S3_ENDPOINT"]
        self.prefix = namespaced_prefix(f"ci-crypt/{uuid.uuid4().hex}")
        self.master_key = new_master_key()
        self._patch = patch.dict(os.environ, self.env, clear=False)
        self._patch.start()
        ensure_versioned_bucket(self.bucket)

    def tearDown(self) -> None:
        try:
            report = cleanup_prefix_versions(
                s3_client(), bucket=self.bucket, prefix=self.prefix
            )
            if not report.ok:
                raise AssertionError(
                    f"Cleanup left leftovers: {report.leftover_keys} ({report.message})"
                )
        finally:
            self._patch.stop()

    def test_crypt_upload_verifies_and_hides_plaintext_names(self) -> None:
        payload = b"crypt-live-payload"
        expected = _sha256_hex(payload)
        relative_path = "secret/report.txt"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rclone_path = root / "rclone.conf"
            _alias, base_remote = write_s3_rclone_config(
                rclone_path,
                endpoint=self.endpoint,
                access_key=self.env["AWS_ACCESS_KEY_ID"],
                secret_key=self.env["AWS_SECRET_ACCESS_KEY"],
                with_plain_alias=False,
            )
            _source, database_path = prepare_crypt_vault(
                root,
                relative_path=relative_path,
                payload=payload,
                bucket=self.bucket,
                prefix=self.prefix,
                base_remote=base_remote,
                master_key=self.master_key,
            )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                aws_region=self.env["AWS_DEFAULT_REGION"],
                rclone_config=str(rclone_path),
                operation_concurrency=1,
                restore_poll_interval=900,
                archive_master_key=self.master_key,
            )
            crypto_settings = SimpleNamespace(archive_master_key=self.master_key)
            rclone_settings = SimpleNamespace(
                rclone_config=str(rclone_path),
                archive_master_key=self.master_key,
            )

            with patch("app.database.settings", database_settings):
                queue_jobs(relative_path, "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.services.rclone_runtime.settings", rclone_settings),
                    patch("app.services.vault_crypto.settings", crypto_settings),
                ):
                    process_jobs_once()

                with SQLiteConnection(str(database_path)) as connection:
                    observed = ArchiveCatalog(connection).get_file_by_path(
                        2, relative_path
                    )
                    job = connection.execute(
                        "SELECT status, message FROM jobs WHERE path=%s",
                        (relative_path,),
                    ).fetchone()
                    version = observed["latest_version"]

                self.assertEqual(job["status"], "completed", job["message"])
                self.assertEqual(version["integrity"], "verified")
                self.assertEqual(version["plaintext_sha256"], expected)
                object_key = version["object_key"]
                self.assertTrue(object_key.startswith(f"{self.prefix.strip('/')}/"))
                self.assertNotIn("report.txt", object_key)
                self.assertNotIn("secret", object_key)
                self.assertFalse(object_key.endswith(".bin"))

                # Object exists under the encrypted key, not the plaintext path.
                head = s3_client().head_object(Bucket=self.bucket, Key=object_key)
                self.assertEqual(head["VersionId"], version["provider_version_id"])
