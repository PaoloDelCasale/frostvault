"""Stop infinite upload retries when the Local Copy changes after S3 linkage (#296).

Seams under test:
- process_upload / process_jobs_once: verify snapshot A from the persisted digest.
- source_changed retry policy: bounded stall with a terminal Job record.
- ArchiveCatalog.queue_jobs: admit B after the previous Job concludes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Windows test hosts lack the Linux open flags imported by vault decommission.
for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.storage import UPLOAD_RETRY_MAX_ATTEMPTS, process_jobs_once
from tests.test_upload_verification import _prepare_plain_vault, _sha256_hex


def _force_retry_due(database_path: Path, relative_path: str = "report.txt") -> None:
    with SQLiteConnection(str(database_path)) as connection:
        connection.execute(
            "UPDATE jobs SET retry_after='2000-01-01T00:00:00+00:00' WHERE path=%s",
            (relative_path,),
        )


def _job_row(database_path: Path, relative_path: str = "report.txt"):
    with SQLiteConnection(str(database_path)) as connection:
        return connection.execute(
            """
            SELECT status, retry_count, archive_version_id,
                   upload_plaintext_sha256, message, message_key
            FROM jobs WHERE path=%s
            """,
            (relative_path,),
        ).fetchone()


class SourceChangeAfterLinkTests(unittest.TestCase):
    def _run_worker(
        self,
        database_path: Path,
        *,
        run_rclone,
        run_rclone_stream,
        version_id: str,
        content_length: int,
    ) -> None:
        database_settings = SimpleNamespace(
            db_backend="sqlite",
            sqlite_path=str(database_path),
        )
        worker_settings = SimpleNamespace(
            operation_concurrency=1,
            restore_poll_interval=900,
        )
        with (
            patch("app.database.settings", database_settings),
            patch("app.storage.settings", worker_settings),
            patch("app.storage.validate_cloud_vault"),
            patch("app.storage.rclone_remote_is_crypt", return_value=False),
            patch("app.storage.run_rclone", side_effect=run_rclone),
            patch("app.storage.run_rclone_stream", side_effect=run_rclone_stream),
            patch(
                "app.storage.s3_client",
                return_value=SimpleNamespace(
                    head_object=lambda **_: {
                        "VersionId": version_id,
                        "ContentLength": content_length,
                        "StorageClass": "STANDARD",
                        "ETag": '"etag"',
                    }
                ),
            ),
        ):
            process_jobs_once()

    def test_source_change_after_copy_verifies_a_without_new_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"snapshot-a"
            mutated = b"snapshot-b-after-copy"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=original
            )
            local = source / "report.txt"
            uploads: list[str] = []
            streams: list[str] = []

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                uploads.append(command[0] if command else "")
                if command[:1] == ("copyto",):
                    local.write_bytes(mutated)

            def fake_stream(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                streams.append(command[0] if command else "")
                kwargs["on_chunk"](original)
                return len(original)

            with patch(
                "app.database.settings",
                SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path)),
            ):
                queue_jobs("report.txt", "upload", 2, 1)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=fake_stream,
                version_id="s3-a",
                content_length=len(original),
            )
            for _ in range(7):
                _force_retry_due(database_path)
                self._run_worker(
                    database_path,
                    run_rclone=fake_upload,
                    run_rclone_stream=fake_stream,
                    version_id="s3-a",
                    content_length=len(original),
                )

            job = _job_row(database_path)
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
                versions = connection.execute(
                    "SELECT COUNT(*) AS total FROM archive_versions"
                ).fetchone()["total"]
                local_copy = connection.execute(
                    "SELECT matched_archive_version_id FROM local_copies"
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["retry_count"] or 0, 0)
            self.assertEqual(job["upload_plaintext_sha256"], _sha256_hex(original))
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                _sha256_hex(original),
            )
            self.assertEqual(versions, 1)
            self.assertEqual(uploads, ["copyto"])
            self.assertEqual(streams, ["cat"])
            self.assertIsNone(local_copy["matched_archive_version_id"])

    def test_source_change_after_link_verifies_a_then_archives_b(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"alpha-bytes"
            mutated = b"beta-bytes-are-longer"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=original
            )
            local = source / "report.txt"
            uploads: list[str] = []
            streams: list[str] = []
            attempts = 0

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                uploads.append(command[0] if command else "")

            def flaky_then_original(*args, **kwargs) -> int:
                nonlocal attempts
                attempts += 1
                command = tuple(str(arg) for arg in args if not callable(arg))
                streams.append(command[0] if command else "")
                if attempts == 1:
                    raise RuntimeError("connection reset by peer")
                kwargs["on_chunk"](original)
                return len(original)

            with patch(
                "app.database.settings",
                SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path)),
            ):
                queue_jobs("report.txt", "upload", 2, 1)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=flaky_then_original,
                version_id="s3-a",
                content_length=len(original),
            )
            local.write_bytes(mutated)
            _force_retry_due(database_path)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=flaky_then_original,
                version_id="s3-a",
                content_length=len(original),
            )

            first = _job_row(database_path)
            self.assertEqual(first["status"], "completed")
            self.assertEqual(uploads, ["copyto"])
            self.assertEqual(streams, ["cat", "cat"])

            def upload_b(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                uploads.append(command[0] if command else "")

            def stream_b(*args, **kwargs) -> int:
                command = tuple(str(arg) for arg in args if not callable(arg))
                streams.append(command[0] if command else "")
                kwargs["on_chunk"](mutated)
                return len(mutated)

            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                job_ids, _, eligible = catalog.queue_jobs(
                    vault_id=2,
                    path="report.txt",
                    action="upload",
                    requested_by=1,
                    requested_at="2026-07-21T12:00:00+00:00",
                    group_id="archive-b",
                    is_directory=False,
                )
                row = catalog.list_file_rows(2)[0]
            self.assertEqual(len(job_ids), 1)
            self.assertEqual(eligible, 1)
            self.assertTrue(row["upload_eligible"])

            self._run_worker(
                database_path,
                run_rclone=upload_b,
                run_rclone_stream=stream_b,
                version_id="s3-b",
                content_length=len(mutated),
            )
            with SQLiteConnection(str(database_path)) as connection:
                versions = connection.execute(
                    """
                    SELECT provider_version_id, integrity, plaintext_sha256
                    FROM archive_versions
                    ORDER BY version_number
                    """
                ).fetchall()
                jobs = connection.execute(
                    "SELECT status FROM jobs ORDER BY id"
                ).fetchall()
                file_ids = {
                    row["vault_file_id"]
                    for row in connection.execute(
                        "SELECT vault_file_id FROM archive_versions"
                    ).fetchall()
                }
            self.assertEqual(len(versions), 2)
            self.assertEqual(versions[0]["provider_version_id"], "s3-a")
            self.assertEqual(versions[0]["plaintext_sha256"], _sha256_hex(original))
            self.assertEqual(versions[1]["provider_version_id"], "s3-b")
            self.assertEqual(versions[1]["plaintext_sha256"], _sha256_hex(mutated))
            self.assertTrue(all(row["integrity"] == "verified" for row in versions))
            self.assertEqual([job["status"] for job in jobs], ["completed", "completed"])
            self.assertEqual(len(file_ids), 1)
            self.assertEqual(uploads, ["copyto", "copyto"])

    def test_source_change_during_verification_does_not_mark_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"verify-a"
            mutated = b"mutated-during-verify"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=original
            )
            local = source / "report.txt"
            uploads: list[str] = []

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                uploads.append(command[0] if command else "")

            def mutating_stream(*args, **kwargs) -> int:
                local.write_bytes(mutated)
                kwargs["on_chunk"](original)
                return len(original)

            with patch(
                "app.database.settings",
                SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path)),
            ):
                queue_jobs("report.txt", "upload", 2, 1)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=mutating_stream,
                version_id="s3-a",
                content_length=len(original),
            )
            job = _job_row(database_path)
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
                local_copy = connection.execute(
                    "SELECT matched_archive_version_id FROM local_copies"
                ).fetchone()
            self.assertEqual(job["status"], "completed")
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                _sha256_hex(original),
            )
            self.assertEqual(uploads, ["copyto"])
            self.assertIsNone(local_copy["matched_archive_version_id"])

    def test_source_disappeared_after_link_still_verifies_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"keep-a"
            source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=original
            )
            local = source / "report.txt"
            attempts = 0
            uploads: list[str] = []

            def fake_upload(*args, **kwargs) -> None:
                command = tuple(str(arg) for arg in args if not callable(arg))
                uploads.append(command[0] if command else "")

            def flaky_stream(*args, **kwargs) -> int:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("connection reset by peer")
                kwargs["on_chunk"](original)
                return len(original)

            with patch(
                "app.database.settings",
                SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path)),
            ):
                queue_jobs("report.txt", "upload", 2, 1)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=flaky_stream,
                version_id="s3-a",
                content_length=len(original),
            )
            local.unlink()
            _force_retry_due(database_path)
            self._run_worker(
                database_path,
                run_rclone=fake_upload,
                run_rclone_stream=flaky_stream,
                version_id="s3-a",
                content_length=len(original),
            )
            job = _job_row(database_path)
            with SQLiteConnection(str(database_path)) as connection:
                observed = ArchiveCatalog(connection).get_file_by_path(2, "report.txt")
            self.assertEqual(job["status"], "completed")
            self.assertEqual(observed["latest_version"]["integrity"], "verified")
            self.assertEqual(
                observed["latest_version"]["plaintext_sha256"],
                _sha256_hex(original),
            )
            self.assertEqual(uploads, ["copyto"])
            self.assertFalse(local.exists())

    def test_unstable_source_before_link_fails_after_stall_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"never-stable"
            _source, database_path = _prepare_plain_vault(
                root, relative_path="report.txt", payload=payload
            )
            uploads: list[str] = []

            def fake_upload(*args, **kwargs) -> None:
                uploads.append("copyto")

            def fake_stream(*args, **kwargs) -> int:
                raise AssertionError("verification must not run before linkage")

            with patch(
                "app.database.settings",
                SimpleNamespace(db_backend="sqlite", sqlite_path=str(database_path)),
            ):
                queue_jobs("report.txt", "upload", 2, 1)

            with patch(
                "app.storage.hash_stable_regular_file",
                side_effect=RuntimeError("Local file changed since fingerprinting"),
            ):
                for _ in range(UPLOAD_RETRY_MAX_ATTEMPTS + 1):
                    self._run_worker(
                        database_path,
                        run_rclone=fake_upload,
                        run_rclone_stream=fake_stream,
                        version_id="s3-a",
                        content_length=len(payload),
                    )
                    _force_retry_due(database_path)

            job = _job_row(database_path)
            with SQLiteConnection(str(database_path)) as connection:
                versions = connection.execute(
                    "SELECT COUNT(*) AS total FROM archive_versions"
                ).fetchone()["total"]
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["message_key"], "job.source_changed_stalled")
            self.assertEqual(job["retry_count"], UPLOAD_RETRY_MAX_ATTEMPTS)
            self.assertIn("stopped after", (job["message"] or "").lower())
            self.assertIsNone(job["archive_version_id"])
            self.assertEqual(versions, 0)
            self.assertEqual(uploads, [])


if __name__ == "__main__":
    unittest.main()
