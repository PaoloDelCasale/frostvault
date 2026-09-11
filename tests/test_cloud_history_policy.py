"""Cloud History Policy: Current Snapshot creation and overwrite purge."""

from __future__ import annotations

import hashlib
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
from app.database import SQLiteConnection
from app.main import queue_jobs
from app.services.cloud_history import (
    ARCHIVE_HISTORY,
    CURRENT_SNAPSHOT,
    InvalidCloudHistoryPolicy,
    keep_discovered_snapshot_version,
    normalize_cloud_history_policy,
)
from app.config import Settings
from app.services import source_layout
from app.services import vaults as vaults_service
from app.storage import process_jobs_once
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class CloudHistoryPolicyUnitTests(unittest.TestCase):
    def test_normalize_defaults_to_archive_history(self) -> None:
        self.assertEqual(normalize_cloud_history_policy(None), ARCHIVE_HISTORY)
        self.assertEqual(normalize_cloud_history_policy(" "), ARCHIVE_HISTORY)

    def test_normalize_required_rejects_omission(self) -> None:
        with self.assertRaises(InvalidCloudHistoryPolicy):
            normalize_cloud_history_policy(None, required=True)

    def test_normalize_rejects_unknown_values(self) -> None:
        with self.assertRaises(InvalidCloudHistoryPolicy):
            normalize_cloud_history_policy("unversioned")

    def test_scan_keep_prefers_catalogued_snapshot(self) -> None:
        self.assertFalse(
            keep_discovered_snapshot_version(
                provider_version_id="s3-b",
                catalog_keep_provider_version_id="s3-a",
                already_kept_provider_version_id=None,
            )
        )
        self.assertTrue(
            keep_discovered_snapshot_version(
                provider_version_id="s3-a",
                catalog_keep_provider_version_id="s3-a",
                already_kept_provider_version_id=None,
            )
        )

    def test_scan_keep_first_candidate_when_empty(self) -> None:
        self.assertTrue(
            keep_discovered_snapshot_version(
                provider_version_id="s3-newest",
                catalog_keep_provider_version_id=None,
                already_kept_provider_version_id=None,
            )
        )
        self.assertFalse(
            keep_discovered_snapshot_version(
                provider_version_id="s3-older",
                catalog_keep_provider_version_id=None,
                already_kept_provider_version_id="s3-newest",
            )
        )


class CloudHistoryPolicyCreationTests(unittest.TestCase):
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
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'alice', 'Alice', 'hash', 0)"
            )
        self.settings = replace(
            Settings(),
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            vault_s3_bucket="test-bucket",
            vault_rclone_remote="test-remote",
        )
        for target in ("app.database.settings", "app.services.vaults.settings"):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _vaults(self) -> list[dict]:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute("SELECT * FROM vaults").fetchall()
    def test_omitted_policy_persists_archive_history(self) -> None:
        vault = vaults_service.create_vault_for_user(1, "History Archive")
        self.assertEqual(vault["cloud_history_policy"], ARCHIVE_HISTORY)

    def test_current_snapshot_is_persisted(self) -> None:
        vault = vaults_service.create_vault_for_user(
            1,
            "Snapshot Archive",
            cloud_history_policy=CURRENT_SNAPSHOT,
        )
        self.assertEqual(vault["cloud_history_policy"], CURRENT_SNAPSHOT)
        stored = self._vaults()[0]
        self.assertEqual(stored["cloud_history_policy"], CURRENT_SNAPSHOT)

    def test_interactive_requirement_rejects_omission(self) -> None:
        with self.assertRaises(vaults_service.InvalidVaultName):
            vaults_service.create_vault_for_user(
                1,
                "Needs Choice",
                require_cloud_history_policy=True,
            )


class _FakeS3:
    def __init__(self) -> None:
        self.versions: dict[str, set[str]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.head_version_id = "missing"
        self.head_size = 0

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
        with SQLiteConnection(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO users(id, username, display_name, password_hash, is_admin)
                VALUES (1, 'owner', 'Owner', 'hash', TRUE)
                """
            )
            connection.execute(
                """
                INSERT INTO vaults(
                    id, slug, name, source_root, s3_bucket, s3_prefix,
                    rclone_remote, cloud_history_policy
                ) VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote', %s)
                """,
                (str(source), CURRENT_SNAPSHOT),
            )
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
    ) -> None:
        def fake_rclone(*args, **kwargs) -> None:
            return None

        def fake_stream(*args, **kwargs) -> int:
            kwargs["on_chunk"](payload)
            return len(payload)

        fake_s3.head_version_id = version_id
        fake_s3.head_size = len(payload)
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
                patch("app.storage.rclone_remote_is_crypt", return_value=False),
                patch("app.storage.vault_encrypts_content", return_value=False),
                patch("app.storage.vault_encrypts_names", return_value=False),
                patch("app.storage.run_rclone", side_effect=fake_rclone),
                patch("app.storage.run_rclone_stream", side_effect=fake_stream),
                patch("app.storage.s3_client", return_value=fake_s3),
            ):
                process_jobs_once()

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
            with SQLiteConnection(str(database_path)) as connection:
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
            with SQLiteConnection(str(database_path)) as connection:
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
