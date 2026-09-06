"""Archive a changed Local Copy of a Vault File already present on S3 (#295).

Seams under test:
- ArchiveCatalog.queue_jobs / list_file_rows: admit B when A exists.
- Directory aggregates: upload action for unmatched local modifications.
- process_upload: new Archive Version for B, reuse/resume without duplicates.
"""

from __future__ import annotations

import hashlib
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
from app.services import directory_aggregates as aggregates
from app.storage import process_jobs_once
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _seed_user_vault(connection: SQLiteConnection, source: Path) -> None:
    connection.execute(
        """
        INSERT INTO users(id, username, display_name, password_hash, is_admin)
        VALUES (1, 'owner', 'Owner', 'hash', TRUE)
        """
    )
    connection.execute(
        """
        INSERT INTO vaults(
            id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
        ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')
        """,
        (str(source),),
    )


def _record_verified(
    catalog: ArchiveCatalog,
    *,
    path: str,
    payload: bytes,
    provider_version_id: str,
    uploaded_at: str,
) -> str:
    version_id = catalog.record_archive_version(
        vault_id=2,
        path=path,
        object_key=f"docs/{path}",
        provider_version_id=provider_version_id,
        size=len(payload),
        storage_class="STANDARD",
        etag="etag",
        uploaded_at=uploaded_at,
        observed_at=uploaded_at,
        scan_id=uploaded_at,
    )
    catalog.mark_version_verified(
        version_id,
        plaintext_sha256=_sha(payload),
        verified_at=uploaded_at,
    )
    return version_id


class ChangedContentAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source"
        self.source.mkdir()
        self.database_path = Path(self.tmp.name) / "catalog.db"
        self.assertEqual(run_alembic(self.database_path).returncode, 0)

    def _open(self) -> SQLiteConnection:
        connection = SQLiteConnection(str(self.database_path))
        connection.__enter__()
        self.addCleanup(connection.__exit__, None, None, None)
        return connection

    def test_changed_content_can_queue_second_archive_version(self) -> None:
        payload_a = b"alpha"
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=len(payload_a),
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_a = _record_verified(
            catalog,
            path="report.txt",
            payload=payload_a,
            provider_version_id="s3-a",
            uploaded_at="2026-07-21T10:00:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=2,
            path="report.txt",
            plaintext_sha256=_sha(payload_a),
            matched_archive_version_id=version_a,
        )
        file_id = catalog.get_file_by_path(2, "report.txt")["id"]
        none, _, _ = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:01:00+00:00",
            group_id="protected",
            is_directory=False,
        )
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=len(payload_a) + 4,
            mtime_ns=2,
            observed_at="2026-07-21T10:02:00+00:00",
        )
        job_ids, _, eligible = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:03:00+00:00",
            group_id="changed",
            is_directory=False,
        )
        row = catalog.list_file_rows(2)[0]
        after = catalog.get_file_by_path(2, "report.txt")
        self.assertEqual(none, [])
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(eligible, 1)
        self.assertEqual(row["state"], "both")
        self.assertTrue(row["upload_eligible"])
        self.assertFalse(row["cleanup_eligible"])
        self.assertEqual(after["id"], file_id)
        self.assertIsNone(after["local_copy"]["plaintext_sha256"])

    def test_protected_content_does_not_queue_duplicate(self) -> None:
        payload = b"same-bytes"
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=len(payload),
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_id = _record_verified(
            catalog,
            path="report.txt",
            payload=payload,
            provider_version_id="s3-a",
            uploaded_at="2026-07-21T10:00:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=2,
            path="report.txt",
            plaintext_sha256=_sha(payload),
            matched_archive_version_id=version_id,
        )
        job_ids, _, eligible = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:01:00+00:00",
            group_id="dup",
            is_directory=False,
        )
        row = catalog.list_file_rows(2)[0]
        self.assertEqual(job_ids, [])
        self.assertEqual(eligible, 0)
        self.assertFalse(row["upload_eligible"])
        self.assertTrue(row["cleanup_eligible"])

    def test_list_and_aggregates_mark_changed_content_upload_eligible(self) -> None:
        payload = b"alpha"
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="notes/report.txt",
            file_type="regular",
            size=len(payload),
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        version_id = _record_verified(
            catalog,
            path="notes/report.txt",
            payload=payload,
            provider_version_id="s3-a",
            uploaded_at="2026-07-21T10:00:00+00:00",
        )
        catalog.set_local_fingerprint(
            vault_id=2,
            path="notes/report.txt",
            plaintext_sha256=_sha(payload),
            matched_archive_version_id=version_id,
        )
        aggregates.flush_directory_aggregates(connection, vault_id=2)
        before_dir = catalog.list_files_page(2)["items"][0]
        before_file = catalog.list_files_page(2, directory="notes")["items"][0]
        self.assertEqual(before_dir["available_actions"]["upload"], 0)
        self.assertFalse(before_file["upload_eligible"])

        catalog.observe_local_copy(
            vault_id=2,
            path="notes/report.txt",
            file_type="regular",
            size=len(payload) + 8,
            mtime_ns=2,
            observed_at="2026-07-21T10:02:00+00:00",
        )
        aggregates.flush_directory_aggregates(connection, vault_id=2)
        after_dir = catalog.list_files_page(2)["items"][0]
        after_file = catalog.list_files_page(2, directory="notes")["items"][0]
        self.assertEqual(after_dir["available_actions"]["upload"], 1)
        self.assertEqual(after_dir["state"], "both")
        self.assertTrue(after_file["upload_eligible"])
        self.assertEqual(after_file["state"], "both")

    def test_unverified_and_mismatch_terminal_versions_are_upload_eligible(self) -> None:
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=4,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        unverified = catalog.record_archive_version(
            vault_id=2,
            path="report.txt",
            object_key="docs/report.txt",
            provider_version_id="s3-unverified",
            size=4,
            storage_class="STANDARD",
            etag="etag",
            uploaded_at="2026-07-21T10:00:00+00:00",
            observed_at="2026-07-21T10:00:00+00:00",
            scan_id="scan-u",
        )
        row = catalog.list_file_rows(2)[0]
        job_ids, _, eligible = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:01:00+00:00",
            group_id="retry-unverified",
            is_directory=False,
        )
        self.assertTrue(row["upload_eligible"])
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(eligible, 1)
        connection.execute(
            "UPDATE jobs SET status='failed' WHERE id=%s", (job_ids[0],)
        )
        catalog.mark_version_mismatch(
            unverified,
            plaintext_sha256=_sha(b"bad!"),
            checked_at="2026-07-21T10:02:00+00:00",
        )
        retry_ids, _, retry_eligible = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:03:00+00:00",
            group_id="retry-mismatch",
            is_directory=False,
        )
        self.assertEqual(len(retry_ids), 1)
        self.assertEqual(retry_eligible, 1)
        still = connection.execute(
            "SELECT integrity FROM archive_versions WHERE id=%s",
            (unverified,),
        ).fetchone()
        self.assertEqual(still["integrity"], "mismatch")

    def test_active_job_still_blocks_second_upload(self) -> None:
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=4,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        first, _, _ = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:01:00+00:00",
            group_id="first",
            is_directory=False,
        )
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=8,
            mtime_ns=2,
            observed_at="2026-07-21T10:02:00+00:00",
        )
        second, _, eligible = catalog.queue_jobs(
            vault_id=2,
            path="report.txt",
            action="upload",
            requested_by=1,
            requested_at="2026-07-21T10:03:00+00:00",
            group_id="second",
            is_directory=False,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(eligible, 0)

    def test_three_versions_remain_recoverable_on_the_same_vault_file(self) -> None:
        connection = self._open()
        _seed_user_vault(connection, self.source)
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=2,
            path="report.txt",
            file_type="regular",
            size=1,
            mtime_ns=1,
            observed_at="2026-07-21T10:00:00+00:00",
        )
        file_id = catalog.get_file_by_path(2, "report.txt")["id"]
        for label, payload, mtime in (
            ("a", b"A", 1),
            ("b", b"BB", 2),
            ("c", b"CCC", 3),
        ):
            catalog.observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=len(payload),
                mtime_ns=mtime,
                observed_at=f"2026-07-21T10:0{mtime}:00+00:00",
            )
            version_id = _record_verified(
                catalog,
                path="report.txt",
                payload=payload,
                provider_version_id=f"s3-{label}",
                uploaded_at=f"2026-07-21T10:0{mtime}:00+00:00",
            )
            catalog.set_local_fingerprint(
                vault_id=2,
                path="report.txt",
                plaintext_sha256=_sha(payload),
                matched_archive_version_id=version_id,
            )
        observed = catalog.get_file_by_path(2, "report.txt")
        versions = catalog.list_versions(2, "report.txt")
        self.assertEqual(observed["id"], file_id)
        self.assertEqual(len(versions), 3)
        self.assertTrue(all(row["recoverable"] for row in versions))
        self.assertEqual(
            [row["provider_version_id"] for row in versions],
            ["s3-c", "s3-b", "s3-a"],
        )


class ChangedContentUploadWorkerTests(unittest.TestCase):
    def _prepare(self, root: Path, payload: bytes) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        target = source / "report.txt"
        target.write_bytes(payload)
        database_path = root / "catalog.db"
        self.assertEqual(run_alembic(database_path).returncode, 0)
        with SQLiteConnection(str(database_path)) as connection:
            _seed_user_vault(connection, source)
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path="report.txt",
                file_type="regular",
                size=len(payload),
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-21T10:00:00+00:00",
            )
        return source, database_path

    def _run_upload(
        self,
        database_path: Path,
        *,
        payload: bytes,
        version_id: str,
        crypt: bool = False,
    ) -> list[str]:
        commands: list[str] = []

        def fake_rclone(*args, **kwargs) -> None:
            command = tuple(str(arg) for arg in args if not callable(arg))
            commands.append(command[0] if command else "")

        def fake_stream(*args, **kwargs) -> int:
            command = tuple(str(arg) for arg in args if not callable(arg))
            commands.append(command[0] if command else "")
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
            queued = queue_jobs("report.txt", "upload", 2, 1)
            self.assertEqual(queued["item_count"], 1)
            with (
                patch("app.storage.settings", worker_settings),
                patch("app.storage.validate_cloud_vault"),
                patch("app.storage.rclone_remote_is_crypt", return_value=crypt),
                patch("app.storage.vault_encrypts_content", return_value=crypt),
                patch("app.storage.vault_encrypts_names", return_value=False),
                patch("app.storage.run_rclone", side_effect=fake_rclone),
                patch("app.storage.run_rclone_stream", side_effect=fake_stream),
                patch(
                    "app.storage.s3_client",
                    return_value=SimpleNamespace(
                        head_object=lambda **_: {
                            "VersionId": version_id,
                            "ContentLength": len(payload),
                            "StorageClass": "STANDARD",
                            "ETag": '"etag"',
                        }
                    ),
                ),
            ):
                process_jobs_once()
        return commands

    def test_changed_content_uploads_a_second_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_a = b"alpha-bytes"
            payload_b = b"beta-bytes-are-longer"
            source, database_path = self._prepare(root, payload_a)
            commands_a = self._run_upload(
                database_path, payload=payload_a, version_id="s3-a"
            )
            local = source / "report.txt"
            local.write_bytes(payload_b)
            with SQLiteConnection(str(database_path)) as connection:
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=len(payload_b),
                    mtime_ns=local.stat().st_mtime_ns,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
                file_id = ArchiveCatalog(connection).get_file_by_path(
                    2, "report.txt"
                )["id"]
            commands_b = self._run_upload(
                database_path, payload=payload_b, version_id="s3-b"
            )
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                observed = catalog.get_file_by_path(2, "report.txt")
                versions = catalog.list_versions(2, "report.txt")
                jobs = connection.execute(
                    "SELECT status FROM jobs ORDER BY id"
                ).fetchall()
            self.assertEqual(commands_a, ["copyto", "cat"])
            self.assertEqual(commands_b, ["copyto", "cat"])
            self.assertEqual(observed["id"], file_id)
            self.assertEqual(observed["latest_version"]["provider_version_id"], "s3-b")
            self.assertEqual(len(versions), 2)
            self.assertTrue(all(row["recoverable"] for row in versions))
            self.assertEqual([job["status"] for job in jobs], ["completed", "completed"])

    def test_unchanged_digest_relinks_without_new_s3_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"same-bytes"
            source, database_path = self._prepare(root, payload)
            self._run_upload(database_path, payload=payload, version_id="s3-a")
            local = source / "report.txt"
            local.write_bytes(payload)
            with SQLiteConnection(str(database_path)) as connection:
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=len(payload),
                    mtime_ns=local.stat().st_mtime_ns,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
            commands = self._run_upload(
                database_path, payload=payload, version_id="s3-should-not-apply"
            )
            with SQLiteConnection(str(database_path)) as connection:
                versions = connection.execute(
                    """
                    SELECT provider_version_id, integrity FROM archive_versions
                    ORDER BY version_number
                    """
                ).fetchall()
                local_copy = connection.execute(
                    "SELECT matched_archive_version_id FROM local_copies"
                ).fetchone()
            self.assertEqual(commands, [])
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["provider_version_id"], "s3-a")
            self.assertEqual(versions[0]["integrity"], "verified")
            self.assertIsNotNone(local_copy["matched_archive_version_id"])

    def test_unverified_retry_resumes_without_new_s3_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"needs-verify"
            source, database_path = self._prepare(root, payload)
            local = source / "report.txt"
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="s3-unverified",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan-u",
                )
                connection.execute(
                    """
                    INSERT INTO jobs(
                        vault_id, vault_file_id, archive_version_id, path,
                        action, status, requested_by, requested_at, updated_at,
                        total_bytes, upload_plaintext_sha256
                    )
                    SELECT 2, vf.id, %s, 'report.txt', 'upload', 'failed', 1,
                           '2026-07-21T10:01:00+00:00', '2026-07-21T10:01:00+00:00',
                           %s, %s
                    FROM vault_files vf
                    JOIN file_paths fp
                      ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                    WHERE fp.path='report.txt'
                    """,
                    (version_id, len(payload), _sha(payload)),
                )
            commands = self._run_upload(
                database_path, payload=payload, version_id="s3-unverified"
            )
            with SQLiteConnection(str(database_path)) as connection:
                versions = connection.execute(
                    """
                    SELECT provider_version_id, integrity FROM archive_versions
                    """
                ).fetchall()
                jobs = connection.execute(
                    "SELECT status FROM jobs ORDER BY id"
                ).fetchall()
            self.assertEqual(local.read_bytes(), payload)
            self.assertEqual(commands, ["cat"])
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["integrity"], "verified")
            self.assertEqual([job["status"] for job in jobs], ["failed", "completed"])

    def test_mismatch_retry_uploads_new_version_and_keeps_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"retry-after-mismatch"
            _source, database_path = self._prepare(root, payload)
            with SQLiteConnection(str(database_path)) as connection:
                catalog = ArchiveCatalog(connection)
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="s3-mismatch",
                    size=len(payload),
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="scan-m",
                )
                catalog.mark_version_mismatch(
                    version_id,
                    plaintext_sha256=_sha(payload),
                    checked_at="2026-07-21T10:01:00+00:00",
                )
            commands = self._run_upload(
                database_path, payload=payload, version_id="s3-retry"
            )
            with SQLiteConnection(str(database_path)) as connection:
                versions = connection.execute(
                    """
                    SELECT provider_version_id, integrity
                    FROM archive_versions
                    ORDER BY version_number
                    """
                ).fetchall()
            self.assertEqual(commands, ["copyto", "cat"])
            self.assertEqual(len(versions), 2)
            self.assertEqual(versions[0]["provider_version_id"], "s3-mismatch")
            self.assertEqual(versions[0]["integrity"], "mismatch")
            self.assertEqual(versions[1]["provider_version_id"], "s3-retry")
            self.assertEqual(versions[1]["integrity"], "verified")

    def test_abc_plain_and_crypt_keep_three_recoverable_versions(self) -> None:
        for crypt in (False, True):
            with self.subTest(crypt=crypt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                payloads = (b"A", b"BB", b"CCC")
                source, database_path = self._prepare(root, payloads[0])
                local = source / "report.txt"
                for index, payload in enumerate(payloads, start=1):
                    if index > 1:
                        local.write_bytes(payload)
                        with SQLiteConnection(str(database_path)) as connection:
                            ArchiveCatalog(connection).observe_local_copy(
                                vault_id=2,
                                path="report.txt",
                                file_type="regular",
                                size=len(payload),
                                mtime_ns=local.stat().st_mtime_ns,
                                observed_at=f"2026-07-21T1{index}:00:00+00:00",
                            )
                    self._run_upload(
                        database_path,
                        payload=payload,
                        version_id=f"s3-{index}",
                        crypt=crypt,
                    )
                with SQLiteConnection(str(database_path)) as connection:
                    catalog = ArchiveCatalog(connection)
                    versions = catalog.list_versions(2, "report.txt")
                    file_id = catalog.get_file_by_path(2, "report.txt")["id"]
                    ids = {
                        row["vault_file_id"]
                        for row in connection.execute(
                            "SELECT vault_file_id FROM archive_versions"
                        ).fetchall()
                    }
                self.assertEqual(len(versions), 3)
                self.assertTrue(all(row["recoverable"] for row in versions))
                self.assertEqual(ids, {file_id})


if __name__ == "__main__":
    unittest.main()
