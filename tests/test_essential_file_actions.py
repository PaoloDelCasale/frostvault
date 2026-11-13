"""#417: only safe automatic renames may clean up verified cloud content.

Rebuilt model (ADR-0035, #595): a confirmed rename keeps the Vault File and
its Current, and a rename Job copies that exact Current server-side, verifies
the copy and only then removes the old key's version by exact VersionId. Real
scan-side confirmation (``storage.apply_auto_renames``), admission, claim and
worker on disposable PostgreSQL 16 with the synthetic ``VersionedProvider``.

Former cases now owned by ``tests.test_rename_sync`` (same worker, provider):
- failed or uncertain copy never deletes the only copy →
  ``test_a_foreign_write_keeps_every_copy_and_blocks``,
  ``test_a_cold_current_needs_an_explicit_restore``,
  ``test_an_unsent_copy_is_proven_absent_before_a_new_attempt``;
- an interrupted old-version cleanup is resumed, never skipped →
  ``test_a_crash_after_promotion_still_removes_the_old_key_exactly``.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import storage
from app.catalog import ArchiveCatalog, VaultFileNotFound
from app.main import app
from app.services.vaults import VAULT_ROW_SQL
from tests.catalog_support import fingerprint
from tests.test_rename_sync import CopyWorkerFixture
from tests.test_upload_worker import A, sha

NEW = "new/photo.txt"


class EssentialFileActionTests(CopyWorkerFixture):
    def vault(self) -> dict:
        with self.db() as connection:
            return connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=2").fetchone()

    def moved(self, new_path: str = NEW) -> dict:
        """Protect report.txt, then move it on disk; a scan observes both paths."""
        before = self.protected()
        target = self.source / new_path
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.file, target)
        self.file = target
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.mark_local_copy_missing(self.file_id, observed_at=storage.now_iso())
            stat = target.stat()
            self.new_id = catalog.observe_local_copy(
                vault_id=2, path=new_path, file_type="regular", size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, observed_at=storage.now_iso())
        self.old_key, self.new_key = self.key, f"vaults/{self.uuid}/{new_path}"
        return before

    def jobs(self, action: str) -> list[dict]:
        return self.rows("SELECT id, status FROM jobs WHERE action=%s ORDER BY id", action)

    def test_safe_rename_verifies_new_path_before_deleting_old_snapshot_without_duplicates(self) -> None:
        before = self.moved()
        summary = storage.apply_auto_renames(self.vault())
        self.assertEqual((summary["confirmed"], summary["queued"]), (1, 1))
        (job,) = self.jobs("rename")
        self.provider.deleted.clear()
        self.run_job(job["id"])
        self.key = self.new_key
        current = self.assert_current(job["id"], A)
        self.assertNotEqual(current["id"], before["id"])
        self.assertEqual(self.provider.deleted, [(self.old_key, before["provider_version_id"])])
        self.assertEqual((self.provider.copies, self.provider.puts), (1, 1), "a rename never uploads")
        self.assertEqual(self.file.read_bytes(), A)
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            self.assertEqual(str(catalog.get_file_by_path(2, NEW)["id"]), str(self.file_id))
            self.assertEqual(connection.execute("SELECT count(*) n FROM files").fetchone()["n"], 1)
            self.assertEqual([v["provider_version_id"] for v in catalog.list_versions(2, NEW) if v["recoverable"]],
                             [current["provider_version_id"]])

    def test_ambiguous_match_is_a_new_upload_and_never_deletes_cloud_content(self) -> None:
        for extra_present in (False, True):
            with self.subTest(extra_present=extra_present):
                self.setUp()
                before = self.moved()
                with self.db() as connection:
                    catalog = ArchiveCatalog(connection)
                    other = self.source / "other.txt"
                    other.write_bytes(A)
                    extra_id = catalog.observe_local_copy(
                        vault_id=2, path="other.txt", file_type="regular", size=len(A),
                        mtime_ns=other.stat().st_mtime_ns, observed_at=storage.now_iso())
                    fingerprint(catalog, 2, "other.txt", sha(A))
                    fingerprint(catalog, 2, NEW, sha(A))
                    if not extra_present:
                        catalog.mark_local_copy_missing(extra_id, observed_at=storage.now_iso())
                with self.db() as connection, self.assertRaises(VaultFileNotFound):
                    ArchiveCatalog(connection).confirm_file_rename(
                        vault_file_id=self.file_id, new_path=NEW, changed_at=storage.now_iso(), vault_id=2)
                summary = storage.apply_auto_renames(self.vault())
                self.assertEqual(summary["confirmed"], 0)
                self.assertEqual(self.jobs("rename"), [])
                with self.db() as connection:
                    current = ArchiveCatalog(connection).get_file_by_path(2, NEW)
                    self.assertNotEqual(str(current["id"]), str(self.file_id))
                    # The old file keeps its Current: nothing was removed from the cloud.
                    old = ArchiveCatalog(connection).list_versions(2, "report.txt")
                    self.assertEqual([(v["provider_version_id"], v["availability"]) for v in old],
                                     [(before["provider_version_id"], "available")])
                self.assertEqual(self.provider.deleted, [])

    def test_archived_or_busy_destination_is_not_consumed_as_a_provisional_file(self) -> None:
        for state in ("archived", "uploading"):
            with self.subTest(state=state):
                self.setUp()
                self.moved()
                with self.db() as connection:
                    catalog = ArchiveCatalog(connection)
                    destination_id = str(catalog.get_file_by_path(2, NEW)["id"])
                    if state == "archived":
                        catalog.begin_replacement(
                            destination_id, object_key=self.new_key,
                            provider_version_id="independent-copy", size=len(A),
                            storage_class="STANDARD", created_at=storage.now_iso())
                    else:
                        job = connection.execute(
                            "INSERT INTO jobs(vault_id, file_id, action, status, origin) "
                            "VALUES (2, %s, 'upload', 'queued', 'manual') RETURNING id",
                            (destination_id,)).fetchone()
                        connection.execute(
                            "INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, 's3_checksum')",
                            (job["id"],))
                summary = storage.apply_auto_renames(self.vault())
                self.assertEqual(summary["confirmed"], 0)
                self.assertEqual(self.provider.deleted, [])
                with self.db() as connection:
                    self.assertEqual(str(ArchiveCatalog(connection).get_file_by_path(2, NEW)["id"]), destination_id)

    def test_confirmation_rechecks_only_its_fingerprint_group(self) -> None:
        self.moved()
        with self.db() as connection:
            catalog = ArchiveCatalog(connection)
            fingerprint(catalog, 2, NEW, sha(A))
            with patch.object(catalog, "list_rename_candidates", wraps=catalog.list_rename_candidates) as recheck:
                catalog.confirm_file_rename(
                    vault_file_id=self.file_id, new_path=NEW, changed_at=storage.now_iso(), vault_id=2)
            recheck.assert_called_once_with(2, digest=sha(A))

    def test_removed_routes_and_contracts_are_not_exposed(self) -> None:
        paths = app.openapi()["paths"]
        for path in ("/api/cloud-archive", "/api/rename-candidates", "/api/confirm-rename", "/api/confirm-folder-rename", "/api/file-history"):
            self.assertNotIn(path, paths)
        schemas = app.openapi()["components"]["schemas"]
        for schema in ("CloudArchiveRequest", "ConfirmRenameAction", "ConfirmFolderRenameAction", "FileHistoryResponse", "PathHistoryEntry"):
            self.assertNotIn(schema, schemas)


if __name__ == "__main__":
    unittest.main()
