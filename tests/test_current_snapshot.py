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

from tests.fake_bucket_s3 import fake_vault_buckets

import os
import unittest
from unittest.mock import patch

for _flag, _value in (
    ("O_DIRECTORY", 0x10000),
    ("O_NOFOLLOW", 0x20000),
    ("O_CLOEXEC", 0x80000),
):
    if not hasattr(os, _flag):
        setattr(os, _flag, _value)

from app.catalog import ArchiveCatalog

from app.services import source_layout
from app.services import vaults as vaults_service
from tests import test_upload_worker as worker
from tests.fixtures import FrostVaultTestCase
from tests.test_upload_worker import A, B


class SnapshotCreationTests(FrostVaultTestCase):
    owner = {"display_name": "Alice"}

    def setUp(self) -> None:
        super().setUp()
        self.sources_root = self.tmp_path / "sources"
        self.sources_root.mkdir()
        self.addCleanup(source_layout.reset_sources_root_override)
        source_layout.override_sources_root(self.sources_root)
        self.s3 = fake_vault_buckets(self)

    def _vaults(self) -> list[dict]:
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM vaults").fetchall()
    def test_creation_has_no_history_choice_or_column(self) -> None:
        vault = vaults_service.create_vault("Snapshot Archive")
        self.assertNotIn("cloud_history_policy", vault)
        self.assertNotIn("cloud_history_policy", self._vaults()[0])


class CurrentSnapshotUploadTests(worker.UploadWorkerCase):
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
