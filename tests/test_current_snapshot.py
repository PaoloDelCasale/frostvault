"""The only cloud behavior: verified snapshot replacement and fail-closed purge.

Replacement on the rebuilt model (ADR-0035) runs the real upload worker on
disposable PostgreSQL 16 with the synthetic ``VersionedProvider``.

Former cases now owned elsewhere:
- an overwrite removes the previous version exactly after verification →
  ``tests.test_upload_cleanup.test_replacement_deletes_the_exact_superseded_version_before_completion``;
- uncatalogued content or a keeper missing from the listing fails closed →
  ``tests.test_upload_cleanup.test_unknown_object_with_equal_bytes_is_never_adopted_or_deleted``,
  ``test_unknown_object_at_the_keeper_key_blocks_even_the_first_completion``,
  ``test_object_appearing_after_the_freeze_stops_before_any_delete``.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn


import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog
from tests.owner_fixtures import insert_owner

from app.config import Settings
from app.services import source_layout
from app.services import vaults as vaults_service
from tests import test_upload_worker as fixtures
from tests.test_upload_worker import A, B
from tests.test_database import run_alembic


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
            insert_owner(connection, display_name="Alice")
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


class CurrentSnapshotUploadTests(unittest.TestCase):
    setUp = fixtures.UploadWorkerTests.setUp
    db = fixtures.UploadWorkerTests.db
    observe = fixtures.UploadWorkerTests.observe
    write = fixtures.UploadWorkerTests.write
    queue = fixtures.UploadWorkerTests.queue
    claim = fixtures.UploadWorkerTests.claim
    run_job = fixtures.UploadWorkerTests.run_job
    job = fixtures.UploadWorkerTests.job
    file_row = fixtures.UploadWorkerTests.file_row
    rows = fixtures.UploadWorkerTests.rows
    version = fixtures.UploadWorkerTests.version
    assert_current = fixtures.UploadWorkerTests.assert_current

    def protected_then_changed(self) -> dict:
        first = self.queue()
        self.run_job(first)
        previous = self.assert_current(first, A)
        self.write(B)
        return previous

    def assert_previous_kept(self, previous: dict) -> None:
        self.assertEqual(self.provider.deleted, [])
        self.assertIn((self.key, previous["provider_version_id"]), self.provider.objects)
        self.assertEqual(self.file_row()["current_version_id"], previous["id"])
        with self.db() as connection:
            versions = ArchiveCatalog(connection).list_versions(2, "report.txt")
        prior = next(row for row in versions if row["provider_version_id"] == previous["provider_version_id"])
        self.assertTrue(prior["recoverable"])
        self.assertFalse(any(str(row["id"]) != str(previous["id"]) and row["integrity"] == "verified"
                             for row in versions))

    def test_failed_upload_or_verification_preserves_previous_verified_copy(self) -> None:
        for failure in ("upload", "verify"):
            with self.subTest(failure=failure):
                self.setUp()
                previous = self.protected_then_changed()
                if failure == "verify":
                    def corrupt(stage, **kwargs):
                        if stage == "accepted":
                            data, metadata = self.provider.objects[self.key, kwargs["version"]]
                            self.provider.objects[self.key, kwargs["version"]] = (b"X" * len(data), metadata)
                    self.provider.hook = corrupt
                    job_id = self.queue("read_back")
                    self.run_job(job_id)
                else:
                    job_id = self.queue()
                    with patch("app.storage.upload_file", side_effect=RuntimeError("transfer failed")):
                        self.run_job(job_id)
                self.assertNotEqual(self.job(job_id)["status"], "completed")
                self.assert_previous_kept(previous)

    def test_listing_failure_fails_closed(self) -> None:
        previous = self.protected_then_changed()
        job_id = self.queue()
        with patch.object(self.provider, "list_object_versions",
                          side_effect=RuntimeError("listing unavailable")):
            self.run_job(job_id)
        self.assertNotEqual(self.job(job_id)["status"], "completed")
        self.assert_previous_kept(previous)


if __name__ == "__main__":
    unittest.main()
