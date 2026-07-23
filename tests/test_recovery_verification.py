"""Digest-verified recovery of an exact Archive Version (issue #4).

Seams under test:
- Recovery Job worker (`process_recover` / `process_jobs_once`) observed through
  Job status and Local Copy fingerprint via ArchiveCatalog.
- Exact-version download adapter (`download_exact_version_plaintext`): must pin
  S3 VersionId; path-only current-object download is forbidden.
- System boundaries mocked: S3 GetObject/HeadObject and Rclone.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import process_jobs_once
from tests.test_database import run_alembic


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        data = self._payload
        for index in range(0, len(data), chunk_size):
            yield data[index : index + chunk_size]

    def read(self) -> bytes:
        return self._payload


def _prepare_cloud_only_version(
    root: Path,
    *,
    relative_path: str,
    payload: bytes,
    storage_class: str = "STANDARD",
    provider_version_id: str = "s3-version-1",
    object_key: str | None = None,
) -> tuple[Path, Path, str]:
    source = root / "source"
    source.mkdir()
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    digest = _sha256_hex(payload)
    key = object_key or f"docs/{relative_path}"
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO users(
                id, username, display_name, password_hash, is_admin
            ) VALUES (1, 'owner', 'Owner', 'hash', TRUE)
            """
        )
        connection.execute(
            """
            INSERT INTO vaults(
                id, slug, name, source_root, s3_bucket, s3_prefix,
                rclone_remote
            ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
            """,
            (str(source),),
        )
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=1_700_000_000_000_000_000,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        file_row = catalog.get_file_by_path(2, relative_path)
        catalog.mark_local_copy_missing(
            file_row["id"], observed_at="2026-07-21T11:00:00+00:00"
        )
        version_id = catalog.record_archive_version(
            vault_id=2,
            path=relative_path,
            object_key=key,
            provider_version_id=provider_version_id,
            size=len(payload),
            storage_class=storage_class,
            etag="etag",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="2026-07-21T10:00:00+00:00",
            origin="upload",
        )
        catalog.mark_version_verified(
            version_id,
            plaintext_sha256=digest,
            verified_at="2026-07-21T10:01:00+00:00",
        )
    return source, database_path, version_id


def _run_plain_recover(
    database_path: Path,
    *,
    relative_path: str,
    downloaded_bytes: bytes | None,
    provider_version_id: str = "s3-version-1",
    object_key: str = "docs/report.txt",
    storage_class: str = "STANDARD",
    restore_header: str | None = None,
) -> Mock:
    get_calls: list[dict] = []
    head_calls: list[dict] = []

    def head_object(**kwargs):
        head_calls.append(kwargs)
        response = {
            "ContentLength": len(downloaded_bytes or b""),
            "StorageClass": storage_class,
            "VersionId": provider_version_id,
        }
        if restore_header is not None:
            response["Restore"] = restore_header
        return response

    def get_object(**kwargs):
        get_calls.append(kwargs)
        if downloaded_bytes is None:
            raise RuntimeError("GetObject denied in test")
        return {
            "Body": _FakeBody(downloaded_bytes),
            "ContentLength": len(downloaded_bytes),
            "VersionId": kwargs.get("VersionId"),
        }

    client = Mock()
    client.head_object = Mock(side_effect=head_object)
    client.get_object = Mock(side_effect=get_object)
    client.restore_object = Mock()
    client.get_calls = get_calls
    client.head_calls = head_calls

    database_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
        restore_days=3,
        restore_tier="Bulk",
        restore_high_impact_gib=100,
        restore_high_impact_eur=10.0,
        restore_approval_hold_seconds=3600,
        allow_local_delete=False,
    )
    with patch("app.database.settings", database_settings):
        queue_jobs(relative_path, "recover", 2, 1)
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone") as run_rclone,
            patch("app.storage.s3_client", return_value=client),
        ):
            process_jobs_once()
            client.run_rclone = run_rclone
    return client


class PlainRecoveryVerificationTests(unittest.TestCase):
    def test_recover_verifies_digest_before_replacing_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=payload,
            )

            target = source / "report.txt"
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob(".*.restore-*.tmp")), [])
            self.assertTrue(client.get_calls)
            self.assertEqual(client.get_calls[0]["VersionId"], "s3-version-1")
            self.assertEqual(client.get_calls[0]["Key"], "docs/report.txt")
            client.run_rclone.assert_not_called()

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(
                observed["local_copy"]["plaintext_sha256"],
                _sha256_hex(payload),
            )
            file_row = next(row for row in listed if row["path"] == "report.txt")
            self.assertTrue(file_row["cleanup_eligible"])
            self.assertEqual(job["status"], "completed")

    def test_mismatched_recovery_digest_does_not_replace_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=b"tampered-bytes",
            )

            target = source / "report.txt"
            self.assertFalse(target.exists())
            self.assertEqual(list(source.glob("**/.*.restore-*.tmp")), [])

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["local_copy"]["presence"], "missing")
            self.assertEqual(job["status"], "failed")
            self.assertIn("digest", (job["message"] or "").lower())

    def test_path_only_rclone_download_is_never_used_for_plain_recovery(self) -> None:
        """Regression: current-key rclone copyto must not recover plain versions."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"archived-plaintext"
            _source, database_path, _version_id = _prepare_cloud_only_version(
                root,
                relative_path="report.txt",
                payload=payload,
                object_key="docs/report.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="report.txt",
                downloaded_bytes=payload,
            )
            client.run_rclone.assert_not_called()
            self.assertTrue(client.get_calls, msg="Recovery must call GetObject")
            self.assertTrue(
                all("VersionId" in call for call in client.get_calls),
                msg="Recovery must pin VersionId on GetObject",
            )


if __name__ == "__main__":
    unittest.main()


class GlacierRestoreWorkflowTests(unittest.TestCase):
    def test_glacier_version_requests_restore_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cold-archive"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="cold.txt",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )
            client = _run_plain_recover(
                database_path,
                relative_path="cold.txt",
                downloaded_bytes=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )

            client.restore_object.assert_called_once()
            restore_kwargs = client.restore_object.call_args.kwargs
            self.assertEqual(restore_kwargs["VersionId"], "s3-version-1")
            self.assertEqual(restore_kwargs["Key"], "docs/cold.txt")
            self.assertEqual(
                restore_kwargs["RestoreRequest"]["GlacierJobParameters"]["Tier"],
                "Bulk",
            )
            client.get_object.assert_not_called()
            self.assertFalse((source / "cold.txt").exists())

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("cold.txt",),
                ).fetchone()
                version = connection.execute(
                    "SELECT restore_state FROM archive_versions WHERE id=%s",
                    (version_id,),
                ).fetchone()

            self.assertEqual(job["status"], "restoring")
            self.assertIn("cannot be cancelled", (job["message"] or "").lower())
            self.assertEqual(version["restore_state"], "restoring")

    def test_restored_glacier_version_downloads_after_restore_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cold-archive"
            source, database_path, version_id = _prepare_cloud_only_version(
                root,
                relative_path="cold.txt",
                payload=payload,
                storage_class="GLACIER",
                object_key="docs/cold.txt",
            )
            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("cold.txt", "recover", 2, 1)
                with SQLiteConnection(str(database_path)) as connection:
                    connection.execute(
                        "UPDATE jobs SET status='restoring', updated_at=%s",
                        ("2020-01-01T00:00:00+00:00",),
                    )

            client = Mock()
            client.head_object = Mock(
                return_value={
                    "ContentLength": len(payload),
                    "StorageClass": "GLACIER",
                    "VersionId": "s3-version-1",
                    "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Aug 2026 00:00:00 GMT"',
                }
            )
            client.get_object = Mock(
                return_value={"Body": _FakeBody(payload), "ContentLength": len(payload)}
            )
            client.restore_object = Mock()
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=1,
                restore_days=3,
                restore_tier="Bulk",
            )
            with (
                patch("app.database.settings", database_settings),
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch("app.storage.run_rclone"),
                patch("app.storage.s3_client", return_value=client),
            ):
                process_jobs_once()

            client.restore_object.assert_not_called()
            client.get_object.assert_called_once()
            self.assertEqual((source / "cold.txt").read_bytes(), payload)
            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s", ("cold.txt",)
                ).fetchone()
                version = connection.execute(
                    "SELECT restore_state FROM archive_versions WHERE id=%s",
                    (version_id,),
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(version["restore_state"], "available")
