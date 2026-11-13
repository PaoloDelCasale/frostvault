"""Archive a changed Local Copy of a Vault File already present on S3 (#295).

Admission seams on the rebuilt model (ADR-0035): ``ArchiveCatalog.queue_jobs``
and ``list_file_rows`` admit new bytes when Current holds older content, and
directory aggregates offer the upload. Disposable PostgreSQL 16 only.

Former worker cases now owned by the real upload worker suites (one Current
per file; superseded versions are removed exactly after verification):
- changed content uploads a second version / A→B→C keeps only the latest →
  ``tests.test_upload_cleanup.test_replacement_deletes_the_exact_superseded_version_before_completion``,
  ``test_steady_state_keeps_only_current_per_file``;
- three versions recoverable on one file: retired, only Current is
  recoverable → ``tests.test_recovery_versions``;
- an unchanged digest relinks without a new object →
  ``tests.test_upload_worker.test_unchanged_content_is_matched_to_current_without_a_send``;
- an unverified retry resumes without a new object →
  ``tests.test_upload_worker.test_materialized_version_waits_for_verification_and_another_job_finishes_it``;
- a mismatch is kept and blocks instead of being replaced silently →
  ``tests.test_upload_worker.test_contradicting_proof_never_becomes_current``,
  ``tests.test_upload_cleanup.test_proven_mismatch_is_retained_and_blocks_the_cleanup``.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.services import directory_aggregates as aggregates
from tests.catalog_support import fingerprint, protect, seed_vault, sha256_hex
from tests.owner_fixtures import insert_owner
from tests.postgres import PostgresConnection
from tests.test_database import run_alembic


class ChangedContentAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database_path = Path(tmp.name) / "catalog.db"
        self.assertEqual(run_alembic(database_path).returncode, 0)
        self.connection = PostgresConnection(str(database_path))
        self.connection.__enter__()
        self.addCleanup(self.connection.__exit__, None, None, None)
        insert_owner(self.connection)
        seed_vault(self.connection, source_root=str(Path(tmp.name) / "source"))
        self.catalog = ArchiveCatalog(self.connection)

    def observe(self, path: str, size: int, mtime_ns: int) -> str:
        return self.catalog.observe_local_copy(
            vault_id=2, path=path, file_type="regular", size=size, mtime_ns=mtime_ns,
            observed_at="2026-07-21T10:00:00+00:00")

    def protected(self, path: str, payload: bytes) -> str:
        """Observe, fingerprint and protect ``payload``: Current, matched."""
        self.observe(path, len(payload), 1)
        fingerprint(self.catalog, 2, path, sha256_hex(payload))
        return protect(self.catalog, 2, path, sha256_hex(payload))

    def queue(self, path: str = "report.txt") -> tuple[list[int], int]:
        job_ids, _, eligible = self.catalog.queue_jobs(
            vault_id=2, path=path, action="upload", requested_at="2026-07-21T10:03:00+00:00",
            group_id=str(uuid.uuid4()), is_directory=False)
        return job_ids, eligible

    def test_changed_content_can_queue_second_archive_version(self) -> None:
        self.protected("report.txt", b"alpha")
        file_id = self.catalog.get_file_by_path(2, "report.txt")["id"]
        self.assertEqual(self.queue(), ([], 0), "matched content is protected")
        self.observe("report.txt", len(b"alpha") + 4, 2)
        job_ids, eligible = self.queue()
        self.assertEqual((len(job_ids), eligible), (1, 1))
        row = self.catalog.list_file_rows(2)[0]
        self.assertEqual(row["state"], "pending")
        self.assertTrue(row["previous_snapshot_protected"])
        self.assertTrue(row["upload_eligible"])
        self.assertFalse(row["cleanup_eligible"])
        after = self.catalog.get_file_by_path(2, "report.txt")
        self.assertEqual(after["id"], file_id, "the same Vault File gets the new version")
        self.assertIsNone(after["local_copy"]["plaintext_sha256"])

    def test_protected_content_does_not_queue_duplicate(self) -> None:
        self.protected("report.txt", b"same-bytes")
        self.assertEqual(self.queue(), ([], 0))
        row = self.catalog.list_file_rows(2)[0]
        self.assertFalse(row["upload_eligible"])
        self.assertTrue(row["cleanup_eligible"])

    def test_list_and_aggregates_mark_changed_content_upload_eligible(self) -> None:
        self.protected("notes/report.txt", b"alpha")
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        before_dir = self.catalog.list_files_page(2)["items"][0]
        before_file = self.catalog.list_files_page(2, directory="notes")["items"][0]
        self.assertEqual(before_dir["available_actions"]["upload"], 0)
        self.assertFalse(before_file["upload_eligible"])

        self.observe("notes/report.txt", len(b"alpha") + 8, 2)
        aggregates.flush_directory_aggregates(self.connection, vault_id=2)
        after_dir = self.catalog.list_files_page(2)["items"][0]
        after_file = self.catalog.list_files_page(2, directory="notes")["items"][0]
        self.assertEqual((after_dir["available_actions"]["upload"], after_dir["state"]), (1, "pending"))
        self.assertTrue(after_file["upload_eligible"])
        self.assertEqual(after_file["state"], "pending")

    def test_an_unverified_replacement_is_upload_eligible_to_resume(self) -> None:
        file_id = self.observe("report.txt", 4, 1)
        self.catalog.begin_replacement(
            file_id, object_key="vaults/x/report.txt", provider_version_id="s3-unverified",
            size=4, storage_class="STANDARD", created_at="2026-07-21T10:00:00+00:00")
        row = self.catalog.list_file_rows(2)[0]
        self.assertTrue(row["upload_eligible"])
        self.assertEqual(self.queue()[1], 1)

    def test_active_job_still_blocks_second_upload(self) -> None:
        self.observe("report.txt", 4, 1)
        first, _ = self.queue()
        self.observe("report.txt", 8, 2)
        second, eligible = self.queue()
        self.assertEqual(len(first), 1)
        self.assertEqual((second, eligible), ([], 0))


if __name__ == "__main__":
    unittest.main()
