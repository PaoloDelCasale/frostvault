"""End-to-end streamed upload verification (issue #230).

Seams under test:
- Upload Job worker (`process_upload` / `process_jobs_once`) observed through
  Job status and Archive Version Integrity via ArchiveCatalog.
- Binary Rclone stream, S3 HeadObject, source stability, and retry boundaries.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.services.rclone_runtime import RuntimeRcloneConfig
from app.storage import (
    RCLONE_STREAM_CHUNK_BYTES,
    OperationCancelled,
    active_operation_processes,
    cancel_jobs,
    process_jobs_once,
    run_rclone_stream,
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
) -> list[tuple[str, ...]]:
    rclone_calls: list[tuple[str, ...]] = []

    def fake_rclone(*args, **kwargs) -> None:
        command = tuple(str(arg) for arg in args if not callable(arg))
        rclone_calls.append(command)
        if command[:1] != ("copyto",) or len(command) < 3:
            return
        # Uploads are acknowledged by the fake provider; verification uses the
        # binary stream seam below and never writes under the source root.

    def fake_rclone_stream(*args, **kwargs) -> int:
        command = tuple(str(arg) for arg in args if not callable(arg))
        rclone_calls.append(command)
        if command[:1] != ("cat",) or read_back_bytes is None:
            return 0
        on_chunk = kwargs["on_chunk"]
        for offset in range(0, len(read_back_bytes), 3):
            on_chunk(read_back_bytes[offset : offset + 3])
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
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone", side_effect=fake_rclone),
            patch("app.storage.run_rclone_stream", side_effect=fake_rclone_stream),
            patch(
                "app.storage.s3_client",
                return_value=SimpleNamespace(
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
    return rclone_calls


class RcloneStreamingProcessTests(unittest.TestCase):
    def _fake_rclone(self, root: Path, body: str) -> None:
        script = root / "rclone"
        script.write_text(
            "#!/bin/sh\n" + body,
            encoding="utf-8",
        )
        script.chmod(0o755)

    def test_binary_stdout_and_noisy_stderr_are_drained_in_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fake_rclone(
                root,
                "i=0\n"
                "while [ $i -lt 20000 ]; do printf 'e' >&2; i=$((i+1)); done\n"
                "printf '\\377\\000payload'\n",
            )
            chunks: list[bytes] = []
            with patch.dict(
                os.environ,
                {"PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"},
            ):
                run_rclone_stream(
                    "cat",
                    "remote:object",
                    on_chunk=chunks.append,
                )
            self.assertEqual(b"".join(chunks), b"\xff\x00payload")
            self.assertTrue(chunks)
            self.assertLessEqual(max(map(len, chunks)), RCLONE_STREAM_CHUNK_BYTES)

    def test_cancellation_terminates_and_reaps_streaming_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fake_rclone(
                root,
                "while :; do printf 'x'; done\n",
            )
            job_id = 987654

            def cancel_after_first_chunk(chunk: bytes) -> None:
                cancel_jobs([job_id])
                raise OperationCancelled("Upload stopped")

            with patch.dict(
                os.environ,
                {"PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"},
            ):
                with self.assertRaises(OperationCancelled):
                    run_rclone_stream(
                        "cat",
                        "remote:object",
                        on_chunk=cancel_after_first_chunk,
                        job_id=job_id,
                    )
            self.assertNotIn(job_id, active_operation_processes)


class PlainUploadVerificationTests(unittest.TestCase):
    def test_plain_upload_reaches_verified_after_read_back_digest_match(self) -> None:
        """Successful rclone transfer + HeadObject is not enough; read-back must match."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"verified-content"
            expected_digest = _sha256_hex(payload)
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            rclone_calls = _run_plain_upload(
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
            self.assertGreaterEqual(len(rclone_calls), 2)

    def test_head_object_success_alone_never_marks_version_verified(self) -> None:
        """HeadObject after rclone exit must not flip Integrity to verified."""
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
            s3 = SimpleNamespace(
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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=fake_upload),
                    patch("app.storage.run_rclone_stream", side_effect=flaky_stream),
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
            self.assertEqual(job["status"], "completed")
            self.assertEqual(versions, 1)
            self.assertEqual(len(uploads), 1)
            self.assertEqual(len(streams), 2)

    def test_changed_source_after_transfer_leaves_version_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"original-bytes"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            local_file = source / "report.txt"
            rclone_calls: list[tuple[str, ...]] = []

            def mutating_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if Path(origin).is_file() and ":" in destination:
                    local_file.write_bytes(b"mutated-after-upload")
                    return
                if ":" in origin and not Path(origin).exists():
                    target = Path(destination)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)

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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=mutating_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
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

            self.assertEqual(observed["latest_version"]["integrity"], "unverified")
            self.assertEqual(job["status"], "retrying")
            self.assertIn("rescheduled", (job["message"] or "").lower())
            self.assertGreaterEqual(len(rclone_calls), 1)

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

            def flaky_rclone(*args, **kwargs) -> None:
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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=flaky_rclone),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
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

            def cancelling_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                if command[:1] == ("copyto",):
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
                    patch("app.storage.rclone_remote_is_crypt", return_value=False),
                    patch("app.storage.run_rclone", side_effect=cancelling_rclone),
                    patch("app.storage.run_rclone_stream", side_effect=cancelling_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
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


class ContentCryptUploadVerificationTests(unittest.TestCase):
    def test_content_encrypted_remote_stream_hashes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"legacy-content-crypt"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            calls: list[tuple[str, ...]] = []

            def fake_upload(*args, **kwargs) -> None:
                calls.append(tuple(str(arg) for arg in args if not callable(arg)))

            def fake_stream(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                calls.append(command)
                self.assertEqual(command[:2], ("cat", "remote:report.txt"))
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
                    patch("app.storage.rclone_remote_is_crypt", return_value=True),
                    patch("app.storage.vault_encrypts_content", return_value=True),
                    patch("app.storage.vault_encrypts_names", return_value=False),
                    patch("app.storage.run_rclone", side_effect=fake_upload),
                    patch("app.storage.run_rclone_stream", side_effect=fake_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **_: {
                                "VersionId": "content-crypt-v1",
                                "ContentLength": len(payload),
                                "StorageClass": "STANDARD",
                                "ETag": '"etag"',
                            }
                        ),
                    ),
                ):
                    process_jobs_once()

            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
                job = connection.execute(
                    "SELECT status FROM jobs WHERE path=%s", ("report.txt",)
                ).fetchone()
            self.assertEqual(observed["latest_version"]["object_key"], "docs/report.txt.bin")
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(job["status"], "completed")
            self.assertEqual([call[0] for call in calls], ["copyto", "cat"])


class CryptUploadVerificationTests(unittest.TestCase):
    def test_crypt_upload_verifies_via_rclone_without_plaintext_bin_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            payload = b"secret-bytes"
            (source / "report.txt").write_bytes(payload)
            expected_digest = _sha256_hex(payload)
            encrypted_relative = "nq/encrypted-name"
            database_path = root / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
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
                        rclone_remote, encryption_mode,
                        crypt_password_ciphertext, crypt_password2_ciphertext,
                        recovery_custody_confirmed_at
                    ) VALUES (
                        2, 'secret', 'Secret', %s, 'bucket', 'docs', 'base',
                        'crypt', 'cipher-a', 'cipher-b',
                        '2026-07-21T09:00:00+00:00'
                    )
                    """,
                    (str(source),),
                )
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=(source / "report.txt").stat().st_mtime_ns,
                    observed_at="2026-07-21T10:00:00+00:00",
                )

            rclone_calls: list[tuple[str, ...]] = []
            fake_config = RuntimeRcloneConfig(
                path=root / "fake.rclone.conf",
                remote_name="vault",
                config_text="[vault]\ntype = crypt\n",
                secrets=None,
            )
            fake_config.path.write_text(fake_config.config_text, encoding="utf-8")

            @contextmanager
            def fake_vault_rclone_config(_vault):
                yield fake_config

            def fake_rclone(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)
                if command[:1] != ("copyto",):
                    return
                origin, destination = command[1], command[2]
                self.assertTrue(
                    origin.startswith("vault:") or destination.startswith("vault:"),
                    msg=command,
                )

            def fake_rclone_stream(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                rclone_calls.append(command)
                self.assertEqual(command[0], "cat")
                self.assertTrue(command[1].startswith("vault:"), msg=command)
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
                    patch(
                        "app.storage.vault_rclone_config",
                        side_effect=fake_vault_rclone_config,
                    ),
                    patch(
                        "app.storage.encode_object_relative_path",
                        return_value=encrypted_relative,
                    ),
                    patch(
                        "app.storage.secrets_for_vault",
                        return_value=SimpleNamespace(password="p", password2="p2"),
                    ),
                    patch("app.storage.run_rclone", side_effect=fake_rclone),
                    patch("app.storage.run_rclone_stream", side_effect=fake_rclone_stream),
                    patch(
                        "app.storage.s3_client",
                        return_value=SimpleNamespace(
                            head_object=lambda **kwargs: {
                                "VersionId": "crypt-v1",
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
                    "SELECT status FROM jobs WHERE path=%s",
                    ("report.txt",),
                ).fetchone()

            object_key = observed["latest_version"]["object_key"]
            self.assertEqual(object_key, f"docs/{encrypted_relative}")
            self.assertNotIn("report.txt", object_key)
            self.assertFalse(object_key.endswith(".bin"))
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                expected_digest,
            )
            self.assertEqual(job["status"], "completed")
            self.assertGreaterEqual(len(rclone_calls), 2)


if __name__ == "__main__":
    unittest.main()
