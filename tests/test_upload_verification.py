"""End-to-end streamed upload verification (issue #230).

Seams under test:
- Upload Job worker (`process_upload` / `process_jobs_once`) observed through
  Job status and Archive Version Integrity via ArchiveCatalog.
- Binary SDK stream, S3 HeadObject, source stability, and retry boundaries.
"""

from __future__ import annotations

from tests.sdk_transfer_stub import mock_sdk_upload, mock_sdk_read

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from tests.snapshot_s3_stub import VersionedS3Stub
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import (
    OperationCancelled,
    cancel_jobs,
    process_jobs_once,
)
from tests.test_database import run_alembic


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prepare_plain_vault(
    root: Path, *, relative_path: str, payload: bytes
) -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir()
    target = source / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    database_path = root / "catalog.db"
    migrated = run_alembic(database_path)
    assert migrated.returncode == 0, migrated.stderr
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO users(
                id, username, display_name, password_hash
            ) VALUES (1, 'owner', 'Owner', 'hash')
            """
        )
        connection.execute(
            """
            INSERT INTO vaults(
                id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs')
            """,
            (str(source),),
        )
        ArchiveCatalog(connection).observe_local_copy(
            vault_id=2,
            path=relative_path,
            file_type="regular",
            size=len(payload),
            mtime_ns=target.stat().st_mtime_ns,
            observed_at="2026-07-21T10:00:00+00:00",
        )
    return source, database_path


def _run_plain_upload(
    database_path: Path,
    *,
    relative_path: str,
    read_back_bytes: bytes | None,
    exact_version_bytes: bytes | None = None,
) -> list[tuple[str, ...]]:
    upload_calls: list[tuple[str, ...]] = []

    def fake_sdk_upload(*args, **kwargs) -> None:
        command = tuple(str(arg) for arg in args if not callable(arg))
        upload_calls.append(command)
        if command[:1] != ("upload",) or len(command) < 3:
            return
        # Uploads are acknowledged by the fake provider; verification uses the
        # binary stream seam below and never writes under the source root.

    def fake_sdk_read(*args, **kwargs) -> int:
        command = tuple(str(arg) for arg in args if not callable(arg))
        upload_calls.append(command)
        if command[:1] != ("read",) or read_back_bytes is None:
            return 0
        on_chunk = kwargs["on_chunk"]
        content = read_back_bytes if exact_version_bytes is None else exact_version_bytes
        for offset in range(0, len(content), 3):
            on_chunk(content[offset : offset + 3])
        return len(read_back_bytes)

    database_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
    )
    size = len(read_back_bytes) if read_back_bytes is not None else 1
    with patch("app.database.settings", database_settings):
        queue_jobs(relative_path, "upload", 2, 1)
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            mock_sdk_upload( side_effect=fake_sdk_upload),
            mock_sdk_read( side_effect=fake_sdk_read),
            patch(
                "app.storage.s3_client",
                return_value=VersionedS3Stub(
                    version_bytes=read_back_bytes if exact_version_bytes is None else exact_version_bytes,
                    head_object=lambda **_: {
                        "VersionId": "s3-version-1",
                        "ContentLength": size,
                        "StorageClass": "STANDARD",
                        "ETag": '"etag"',
                    }
                ),
            ),
        ):
            process_jobs_once()
    return upload_calls




class PlainUploadVerificationTests(unittest.TestCase):
    def test_response_version_wins_current_key_race_and_unknown_copy_is_not_purged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = b"good"
            _source, path = _prepare_plain_vault(Path(directory), relative_path="report.txt", payload=payload)
            with SQLiteConnection(str(path)) as connection:
                catalog = ArchiveCatalog(connection)
                previous = catalog.record_archive_version(
                    vault_id=2, path="report.txt", object_key="docs/report.txt",
                    provider_version_id="previous-only-copy", size=3, storage_class="STANDARD",
                    etag="old", uploaded_at="2026-07-20", observed_at="2026-07-20", scan_id="old",
                )
                catalog.mark_version_verified(previous, plaintext_sha256=_sha256_hex(b"old"), verified_at="2026-07-20")
            provider = VersionedS3Stub(
                initial_versions={"docs/report.txt": {"previous-only-copy", "concurrent-writer"}},
                version_bytes=payload,
                head_object=lambda **kwargs: {"VersionId": kwargs.get("VersionId") or "concurrent-writer",
                                              "ContentLength": len(payload), "ETag": "etag"},
            )
            with patch("app.database.settings", SimpleNamespace(db_backend="sqlite", sqlite_path=str(path))):
                queue_jobs("report.txt", "upload", 2, 1)
                with patch("app.storage.validate_cloud_vault"), patch("app.storage.s3_client", return_value=provider), patch("app.storage.upload_s3_file", return_value={"VersionId": "upload-response"}):
                    process_jobs_once()
            with SQLiteConnection(str(path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
                job = connection.execute("SELECT status, message FROM jobs").fetchone()
            self.assertEqual({row["provider_version_id"] for row in versions}, {"previous-only-copy", "upload-response"})
            self.assertTrue(all(row["integrity"] == "verified" and row["availability"] == "available" for row in versions))
            self.assertEqual(provider.reads[0]["VersionId"], "upload-response")
            self.assertEqual(provider.deleted, [])
            self.assertEqual(job["status"], "failed")
            self.assertIn("uncatalogued cloud content", job["message"])

    def test_matching_current_key_is_not_proof_of_the_linked_version_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"good"
            _source, path = _prepare_plain_vault(root, relative_path="report.txt", payload=payload)
            with SQLiteConnection(str(path)) as connection:
                catalog = ArchiveCatalog(connection)
                previous = catalog.record_archive_version(
                    vault_id=2, path="report.txt", object_key="docs/report.txt",
                    provider_version_id="previous-only-copy", size=3, storage_class="STANDARD",
                    etag="old", uploaded_at="2026-07-20", observed_at="2026-07-20", scan_id="old",
                )
                catalog.mark_version_verified(previous, plaintext_sha256=_sha256_hex(b"old"), verified_at="2026-07-20")
            _run_plain_upload(path, relative_path="report.txt", read_back_bytes=payload, exact_version_bytes=b"evil")
            with SQLiteConnection(str(path)) as connection:
                catalog = ArchiveCatalog(connection)
                versions = catalog.list_versions(2, "report.txt")
                old = next(row for row in versions if row["id"] == previous)
                current = next(row for row in versions if row["id"] != previous)
                job = connection.execute("SELECT status FROM jobs").fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertEqual(current["integrity"], "mismatch")
            self.assertEqual(old["integrity"], "verified")
            self.assertEqual(old["availability"], "available")

    def test_changed_current_key_does_not_assign_mismatch_to_good_exact_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _source, path = _prepare_plain_vault(root, relative_path="report.txt", payload=b"good")
            _run_plain_upload(path, relative_path="report.txt", read_back_bytes=b"evil", exact_version_bytes=b"good")
            with SQLiteConnection(str(path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
                job = connection.execute("SELECT status FROM jobs").fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(observed["latest_version"]["availability"], "available")

    def test_plain_upload_reaches_verified_after_read_back_digest_match(self) -> None:
        """Successful SDK transfer + HeadObject is not enough; read-back must match."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"verified-content"
            expected_digest = _sha256_hex(payload)
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            upload_calls = _run_plain_upload(
                database_path,
                relative_path="report.txt",
                read_back_bytes=payload,
            )

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                expected_digest,
            )
            self.assertEqual(
                observed["local_copy"]["plaintext_sha256"],
                expected_digest,
            )
            file_row = next(row for row in listed if row["path"] == "report.txt")
            self.assertTrue(file_row["cleanup_eligible"])
            self.assertEqual(job["status"], "completed")
            self.assertGreaterEqual(len(upload_calls), 2)

    def test_head_object_success_alone_never_marks_version_verified(self) -> None:
        """HeadObject after upload completion must not flip Integrity to verified."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"local-bytes"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            _run_plain_upload(
                database_path,
                relative_path="report.txt",
                read_back_bytes=None,
            )

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertIsNotNone(observed["latest_version"])
            self.assertEqual(
                observed["latest_version"]["provider_version_id"],
                "s3-version-1",
            )
            self.assertEqual(observed["latest_version"]["integrity"], "unverified")
            self.assertIsNone(observed["latest_version"]["plaintext_sha256"])
            file_row = next(row for row in listed if row["path"] == "report.txt")
            self.assertFalse(file_row["cleanup_eligible"])
            self.assertEqual(job["status"], "retrying")

    def test_mismatched_read_back_digest_leaves_version_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"local-bytes"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            _run_plain_upload(
                database_path,
                relative_path="report.txt",
                read_back_bytes=b"different-remote-bytes",
            )

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                listed = catalog.list_file_rows(2)
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            self.assertEqual(observed["latest_version"]["integrity"], "mismatch")
            self.assertFalse(
                next(row for row in listed if row["path"] == "report.txt")[
                    "cleanup_eligible"
                ]
            )
            self.assertEqual(job["status"], "failed")
            self.assertIn("digest", (job["message"] or "").lower())

    def test_verification_writes_no_artifact_inside_the_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"read-only-directory-content"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            _run_plain_upload(
                database_path,
                relative_path="report.txt",
                read_back_bytes=payload,
            )
            self.assertEqual(
                sorted(path.name for path in source.iterdir()), ["report.txt"]
            )
            self.assertEqual(list(source.rglob("*.verify-*.tmp")), [])

    def test_verification_retry_reuses_linked_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"retry-verification-without-reupload"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            uploads: list[tuple[str, ...]] = []
            streams: list[tuple[str, ...]] = []
            attempts = 0

            def fake_upload(*args, **kwargs) -> None:
                uploads.append(tuple(str(arg) for arg in args if not callable(arg)))

            def flaky_stream(*args, **kwargs) -> int:
                nonlocal attempts
                attempts += 1
                streams.append(tuple(str(arg) for arg in args if not callable(arg)))
                if attempts == 1:
                    raise RuntimeError("connection reset by peer")
                kwargs["on_chunk"](payload)
                return len(payload)

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            s3 = VersionedS3Stub(
                version_bytes=payload,
                head_object=lambda **_: {
                    "VersionId": "one-version",
                    "ContentLength": len(payload),
                    "StorageClass": "STANDARD",
                    "ETag": '"etag"',
                }
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("report.txt", "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=fake_upload),
                    mock_sdk_read( side_effect=flaky_stream),
                    patch("app.storage.s3_client", return_value=s3),
                ):
                    process_jobs_once()

                    with SQLiteConnection(str(database_path)) as connection:
                        first = connection.execute(
                            "SELECT status, retry_after, archive_version_id FROM jobs WHERE path=%s",
                            ("report.txt",),
                        ).fetchone()
                        connection.execute(
                            "UPDATE jobs SET retry_after='2000-01-01T00:00:00+00:00' WHERE path=%s",
                            ("report.txt",),
                        )
                        count = connection.execute(
                            "SELECT COUNT(*) AS total FROM archive_versions"
                        ).fetchone()["total"]
                    self.assertEqual(first["status"], "retrying")
                    self.assertIsNotNone(first["archive_version_id"])
                    self.assertEqual(count, 1)

                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status, archive_version_id FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()
                versions = connection.execute(
                    "SELECT COUNT(*) AS total FROM archive_versions"
                ).fetchone()["total"]
                local_copy = connection.execute(
                    """
                    SELECT plaintext_sha256, matched_archive_version_id
                    FROM local_copies
                    """
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(versions, 1)
            self.assertEqual(len(uploads), 1)
            self.assertEqual(len(streams), 2)
            self.assertEqual(local_copy["plaintext_sha256"], _sha256_hex(payload))
            self.assertEqual(
                local_copy["matched_archive_version_id"],
                job["archive_version_id"],
            )

    def test_changed_local_copy_between_retries_does_not_mark_remote_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"upload-snapshot"
            mutated = b"changed-after-first-attempt"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=original
            )
            uploads: list[tuple[str, ...]] = []
            streams: list[tuple[str, ...]] = []
            attempts = 0

            def fake_upload(*args, **kwargs) -> None:
                uploads.append(tuple(str(arg) for arg in args if not callable(arg)))

            def flaky_stream(*args, **kwargs) -> int:
                nonlocal attempts
                attempts += 1
                streams.append(tuple(str(arg) for arg in args if not callable(arg)))
                if attempts == 1:
                    raise RuntimeError("connection reset by peer")
                kwargs["on_chunk"](original)
                return len(original)

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            s3 = VersionedS3Stub(
                version_bytes=original,
                head_object=lambda **_: {
                    "VersionId": "one-version",
                    "ContentLength": len(original),
                    "StorageClass": "STANDARD",
                    "ETag": '"etag"',
                }
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("report.txt", "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=fake_upload),
                    mock_sdk_read( side_effect=flaky_stream),
                    patch("app.storage.s3_client", return_value=s3),
                ):
                    process_jobs_once()
                    source.joinpath("report.txt").write_bytes(mutated)

                    with SQLiteConnection(str(database_path)) as connection:
                        connection.execute(
                            "UPDATE jobs SET retry_after='2000-01-01T00:00:00+00:00' WHERE path=%s",
                            ("report.txt",),
                        )
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
                job = connection.execute(
                    """
                    SELECT status, archive_version_id, upload_plaintext_sha256, message
                    FROM jobs WHERE path=%s
                    """,
                    ("report.txt",),
                ).fetchone()
                versions = connection.execute(
                    "SELECT COUNT(*) AS total FROM archive_versions"
                ).fetchone()["total"]
                local_copy = connection.execute(
                    """
                    SELECT plaintext_sha256, matched_archive_version_id
                    FROM local_copies
                    """
                ).fetchone()

            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                _sha256_hex(original),
            )
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["upload_plaintext_sha256"], _sha256_hex(original))
            self.assertIsNotNone(job["archive_version_id"])
            self.assertEqual(versions, 1)
            self.assertEqual(len(uploads), 1)
            self.assertEqual(len(streams), 2)
            self.assertNotEqual(
                local_copy["plaintext_sha256"], _sha256_hex(original)
            )
            self.assertIsNone(local_copy["matched_archive_version_id"])

    def test_changed_source_after_transfer_verifies_uploaded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"original-bytes"
            mutated = b"mutated-after-upload"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            local_file = source / "report.txt"
            upload_calls: list[tuple[str, ...]] = []

            def mutating_sdk_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                upload_calls.append(command)
                if command[:1] != ("upload",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if Path(origin).is_file() and destination == "docs/report.txt":
                    local_file.write_bytes(mutated)

            def fake_stream(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                upload_calls.append(command)
                kwargs["on_chunk"](payload)
                return len(payload)

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("report.txt", "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=mutating_sdk_upload),
                    mock_sdk_read( side_effect=fake_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=VersionedS3Stub(
                            version_bytes=payload,
                            head_object=lambda **_: {
                                "VersionId": "s3-version-1",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            }
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
                job = connection.execute(
                    "SELECT status, message FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()
                local_copy = connection.execute(
                    "SELECT matched_archive_version_id FROM local_copies"
                ).fetchone()

            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                _sha256_hex(payload),
            )
            self.assertEqual(job["status"], "completed")
            self.assertIsNone(local_copy["matched_archive_version_id"])
            self.assertEqual(
                [command[0] for command in upload_calls if command],
                ["upload", "read"],
            )

    def test_empty_and_unicode_paths_verify_end_to_end(self) -> None:
        cases = (
            ("empty.bin", b""),
            ("docs/unicodé café.txt", "πλάνο".encode("utf-8")),
        )
        for relative_path, payload in cases:
            with self.subTest(path=relative_path):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    expected_digest = _sha256_hex(payload)
                    _source, database_path = _prepare_plain_vault(
                        root, relative_path=relative_path, payload=payload
                    )
                    _run_plain_upload(
                        database_path,
                        relative_path=relative_path,
                        read_back_bytes=payload,
                    )
                    with SQLiteConnection(str(database_path)) as connection:
                        observed = ArchiveCatalog(connection).get_file_by_path(
                            2, relative_path
                        )
                        job = connection.execute(
                            "SELECT status FROM jobs WHERE path=%s",
                            (relative_path,),
                        ).fetchone()
                    self.assertEqual(
                        observed["latest_version"]["integrity"], "verified"
                    )
                    self.assertEqual(
                        observed["latest_version"]["plaintext_sha256"],
                        expected_digest,
                    )
                    self.assertEqual(job["status"], "completed")

    def test_transient_upload_failure_enters_retrying_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"retry-me"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )

            def flaky_sdk_upload(*args, **kwargs) -> None:
                raise RuntimeError("SlowDown: Please reduce your request rate")

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queue_jobs("report.txt", "upload", 2, 1)
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=flaky_sdk_upload),
                    patch(
                        "app.storage.s3_client",
                        return_value=VersionedS3Stub(
                            head_object=lambda **_: {
                                "VersionId": "s3-version-1",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            }
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    """
                    SELECT status, retry_count, retry_after, message
                    FROM jobs WHERE path=%s
                    """,
                    ("report.txt",),
                ).fetchone()
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )

            self.assertEqual(job["status"], "retrying")
            self.assertEqual(job["retry_count"], 1)
            self.assertIsNotNone(job["retry_after"])
            self.assertIn("SlowDown", job["message"] or "")
            self.assertIsNone(observed["latest_version"])

    def test_verification_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"cancel-me"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            job_id_holder: dict[str, int] = {}

            def cancelling_sdk_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                if command[:1] == ("upload",):
                    return

            def cancelling_stream(*args, **kwargs) -> int:
                from app.storage import cancel_jobs

                cancel_jobs([job_id_holder["id"]])
                raise OperationCancelled("Upload stopped")

            database_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
            )
            worker_settings = SimpleNamespace(
                operation_concurrency=1,
                restore_poll_interval=900,
            )
            with patch("app.database.settings", database_settings):
                queued = queue_jobs("report.txt", "upload", 2, 1)
                with SQLiteConnection(str(database_path)) as connection:
                    job_id_holder["id"] = connection.execute(
                        "SELECT id FROM jobs WHERE path=%s",
                        ("report.txt",),
                    ).fetchone()["id"]
                with (
                    patch("app.storage.settings", worker_settings),
                    patch("app.storage.validate_cloud_vault"),
                    mock_sdk_upload( side_effect=cancelling_sdk_upload),
                    mock_sdk_read( side_effect=cancelling_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=VersionedS3Stub(
                            head_object=lambda **_: {
                                "VersionId": "s3-version-1",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            }
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()
                observed = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )
            self.assertEqual(queued["item_count"], 1)
            self.assertEqual(job["status"], "cancelled")
            if observed["latest_version"] is not None:
                self.assertNotEqual(
                    observed["latest_version"]["integrity"], "verified"
                )


if __name__ == "__main__":
    unittest.main()
