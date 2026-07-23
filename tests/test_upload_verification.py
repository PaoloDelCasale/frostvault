"""End-to-end upload verification (issue #11).

Seams under test:
- Upload Job worker (`process_upload` / `process_jobs_once`) observed through
  Job status and Archive Version Integrity via ArchiveCatalog.
- System boundaries mocked: Rclone and S3 HeadObject.
"""

from __future__ import annotations

import hashlib
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
from app.storage import OperationCancelled, process_jobs_once
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
        origin, destination = command[1], command[2]
        if read_back_bytes is None:
            return
        if ":" in origin and not Path(origin).exists():
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_back_bytes)

    database_settings = SimpleNamespace(
        db_backend="sqlite",
        sqlite_path=str(database_path),
    )
    worker_settings = SimpleNamespace(
        operation_concurrency=1,
        restore_poll_interval=900,
    )
    size = len(read_back_bytes or b"")
    with patch("app.database.settings", database_settings):
        queue_jobs(relative_path, "upload", 2, 1)
        with (
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone", side_effect=fake_rclone),
            patch(
                "app.storage.s3_client",
                return_value=SimpleNamespace(
                    head_object=lambda **_: {
                        "VersionId": "s3-version-1",
                        "ContentLength": size or 1,
                        "StorageClass": "STANDARD",
                        "ETag": '"etag"',
                    }
                ),
            ),
        ):
            process_jobs_once()
    return rclone_calls


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
            self.assertEqual(job["status"], "failed")

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
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                if Path(origin).is_file() and ":" in destination:
                    return
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
                if command[:1] != ("copyto",) or len(command) < 3:
                    return
                origin, destination = command[1], command[2]
                self.assertTrue(
                    origin.startswith("vault:") or destination.startswith("vault:"),
                    msg=command,
                )
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
