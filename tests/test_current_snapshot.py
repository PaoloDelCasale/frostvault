"""The only cloud behavior: verified snapshot replacement and fail-closed purge."""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

from tests.sdk_transfer_stub import mock_sdk_upload, mock_sdk_read

import hashlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog

from app.main import queue_jobs
from app.config import Settings
from app.services import source_layout
from app.services import vaults as vaults_service
from app.storage import process_jobs_once
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class SnapshotCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.sources_root = Path(self._tmp.name) / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Alice', 'hash')"
            )
        self.settings = replace(
            Settings(),
            database_dsn=database_dsn(str(self.database_path)),
            vault_s3_bucket="test-bucket",

        )
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _vaults(self) -> list[dict]:
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vaults").fetchall()
    def test_creation_has_no_history_choice_or_column(self) -> None:
        vault = vaults_service.create_vault("Snapshot Archive")
        self.assertNotIn("cloud_history_policy", vault)
        self.assertNotIn("cloud_history_policy", self._vaults()[0])


class _FakeS3:
    def __init__(self) -> None:
        self.versions: dict[str, set[str]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.head_version_id = "missing"
        self.head_size = 0
        self.payload = b""

    def get_object(self, *, Bucket, Key, VersionId):
        assert VersionId == self.head_version_id
        assert VersionId in self.versions[Key]
        return {"Body": io.BytesIO(self.payload)}

    def head_object(self, **kwargs):
        key = str(kwargs["Key"])
        self.versions.setdefault(key, set()).add(self.head_version_id)
        return {
            "VersionId": self.head_version_id,
            "ContentLength": self.head_size,
            "StorageClass": "STANDARD",
            "ETag": '"etag"',
        }

    def list_object_versions(self, **kwargs):
        key = str(kwargs.get("Prefix") or "")
        return {
            "Versions": [
                {"Key": key, "VersionId": version_id}
                for version_id in sorted(self.versions.get(key, set()))
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def delete_object(self, **kwargs):
        key = str(kwargs["Key"])
        version_id = str(kwargs["VersionId"])
        self.deleted.append((key, version_id))
        self.versions.get(key, set()).discard(version_id)
        return {"VersionId": version_id}


class CurrentSnapshotUploadTests(unittest.TestCase):
    def _prepare(self, root: Path, payload: bytes) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        target = source / "report.txt"
        target.write_bytes(payload)
        database_path = root / "catalog.db"
        self.assertEqual(run_alembic(database_path).returncode, 0)
        with PostgresConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO owner(id, display_name, password_hash) VALUES (1, 'Owner', 'hash')
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs')
                """,
                (str(source),)
            )
            from app.services.vault_relocation import enroll_vault_root_identity
            enroll_vault_root_identity(connection, 2, str(source))
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
        fake_s3: _FakeS3,
        *,
        payload: bytes,
        version_id: str,
        failure: str | None = None,
    ) -> None:
        def fake_sdk_upload(*args, **kwargs) -> None:
            if failure == "upload":
                raise RuntimeError("transfer failed")

        def fake_stream(*args, **kwargs) -> int:
            data = b"wrong-cloud-bytes" if failure == "verify" else payload
            kwargs["on_chunk"](data)
            return len(data)

        fake_s3.head_version_id = version_id
        fake_s3.head_size = len(payload)
        fake_s3.payload = payload
        database_settings = SimpleNamespace(
            database_dsn=database_dsn(str(database_path)),
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
                mock_sdk_upload( side_effect=fake_sdk_upload),
                mock_sdk_read( side_effect=fake_stream),
                patch("app.storage.s3_client", return_value=fake_s3),
            ):
                process_jobs_once()

    def test_failed_upload_or_verification_preserves_previous_verified_copy(self) -> None:
        for failure in ("upload", "verify"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source, database_path = self._prepare(Path(directory), b"original")
                s3 = _FakeS3()
                self._run_upload(database_path, s3, payload=b"original", version_id="s3-a")
                local = source / "report.txt"
                local.write_bytes(b"replacement")
                with PostgresConnection(str(database_path)) as connection:
                    ArchiveCatalog(connection).observe_local_copy(
                        vault_id=2, path="report.txt", file_type="regular",
                        size=local.stat().st_size, mtime_ns=local.stat().st_mtime_ns,
                        observed_at="2026-11-03T11:00:00+00:00",
                    )
                self._run_upload(database_path, s3, payload=b"replacement", version_id="s3-b", failure=failure)
                self.assertEqual(s3.deleted, [])
                self.assertIn("s3-a", s3.versions["docs/report.txt"])
                with PostgresConnection(str(database_path)) as connection:
                    versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
                prior = next(row for row in versions if row["provider_version_id"] == "s3-a")
                self.assertTrue(prior["recoverable"])
                self.assertFalse(any(row["provider_version_id"] == "s3-b" and row["integrity"] == "verified" for row in versions))

    def test_listing_failure_missing_keeper_and_uncatalogued_content_fail_closed(self) -> None:
        for failure in ("listing", "missing_keeper", "uncatalogued"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source, database_path = self._prepare(Path(directory), b"original")
                s3 = _FakeS3()
                self._run_upload(database_path, s3, payload=b"original", version_id="s3-a")
                local = source / "report.txt"
                local.write_bytes(b"replacement")
                with PostgresConnection(str(database_path)) as connection:
                    ArchiveCatalog(connection).observe_local_copy(
                        vault_id=2, path="report.txt", file_type="regular",
                        size=local.stat().st_size, mtime_ns=local.stat().st_mtime_ns,
                        observed_at="2026-11-03T11:00:00+00:00",
                    )
                if failure == "uncatalogued":
                    s3.versions["docs/report.txt"].add("s3-unknown")
                    listing = s3.list_object_versions
                elif failure == "missing_keeper":
                    listing = lambda **kwargs: {"Versions": [{"Key": "docs/report.txt", "VersionId": "s3-a"}]}
                else:
                    def listing(**kwargs):
                        raise RuntimeError("listing unavailable")
                with patch.object(s3, "list_object_versions", side_effect=listing):
                    self._run_upload(database_path, s3, payload=b"replacement", version_id="s3-b")
                self.assertEqual(s3.deleted, [])
                self.assertIn("s3-a", s3.versions["docs/report.txt"])
                with PostgresConnection(str(database_path)) as connection:
                    versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
                    current_id = connection.execute("SELECT vf.current_version_id FROM vault_files vf JOIN archive_versions av ON av.vault_file_id=vf.id WHERE av.id=%s", (versions[0]["id"],)).fetchone()["current_version_id"]
                prior = next(row for row in versions if row["provider_version_id"] == "s3-a")
                self.assertEqual((prior["integrity"], prior["availability"]), ("verified", "available"))
                self.assertEqual(prior["recoverable"], prior["id"] == current_id)
                # Cleanup failure may follow publication of a verified replacement.
                # Exactly that explicit snapshot is selectable; history is retained.
                keepers = [row for row in versions if row["recoverable"]]
                self.assertEqual(len(keepers), 1)
                self.assertEqual(keepers[0]["id"], current_id)
                self.assertIn(keepers[0]["provider_version_id"], s3.versions["docs/report.txt"])

    def test_overwrite_purges_previous_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_a = b"alpha-bytes"
            payload_b = b"beta-bytes-are-longer"
            source, database_path = self._prepare(root, payload_a)
            fake_s3 = _FakeS3()
            self._run_upload(
                database_path, fake_s3, payload=payload_a, version_id="s3-a"
            )
            local = source / "report.txt"
            local.write_bytes(payload_b)
            with PostgresConnection(str(database_path)) as connection:
                ArchiveCatalog(connection).observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=len(payload_b),
                    mtime_ns=local.stat().st_mtime_ns,
                    observed_at="2026-07-21T11:00:00+00:00",
                )
            self._run_upload(
                database_path, fake_s3, payload=payload_b, version_id="s3-b"
            )
            with PostgresConnection(str(database_path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
                jobs = connection.execute(
                    "SELECT status FROM jobs ORDER BY id"
                ).fetchall()
            recoverable = [row for row in versions if row["recoverable"]]
            self.assertEqual(len(recoverable), 1)
            self.assertEqual(recoverable[0]["provider_version_id"], "s3-b")
            purged = [row for row in versions if row["availability"] == "purged"]
            self.assertEqual(len(purged), 1)
            self.assertEqual(purged[0]["provider_version_id"], "s3-a")
            self.assertIn(("docs/report.txt", "s3-a"), fake_s3.deleted)
            self.assertEqual([job["status"] for job in jobs], ["completed", "completed"])
            self.assertEqual(_sha(payload_b), recoverable[0]["plaintext_sha256"])
