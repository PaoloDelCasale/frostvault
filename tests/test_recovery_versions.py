"""Internal recovery pins target the current verified snapshot, never history.

On the rebuilt model (ADR-0035) a Vault File has Current, at most one
replacement in progress, and superseded versions awaiting exact cleanup.
Only a verified, available Current is recoverable; the others are listed
with the reason they cannot be selected, or not listed at all.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog

from tests.catalog_support import fingerprint, protect, seed_vault
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class VersionSelectionTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, dict[str, str]]:
        """``v-old`` superseded, ``v-new`` Current, ``v-bad`` the replacement in progress."""
        database_path = root / "catalog.db"
        self.assertEqual(run_alembic(database_path).returncode, 0)
        with PostgresConnection(str(database_path)) as connection:
            insert_owner(connection)
            seed_vault(connection, source_root=str(root / "source"))
            catalog = ArchiveCatalog(connection)
            file_id = catalog.observe_local_copy(
                vault_id=2, path="notes.txt", file_type="regular", size=4, mtime_ns=1,
                observed_at="2026-07-20T09:00:00+00:00")
            ids = {}
            for name, payload, stamp in (("v-old", b"old!", "2026-07-20T10:00:00+00:00"),
                                         ("v-new", b"new!", "2026-07-21T10:00:00+00:00")):
                fingerprint(catalog, 2, "notes.txt", _sha(payload))
                ids[name] = protect(catalog, 2, "notes.txt", _sha(payload), provider_version_id=name,
                                    created_at=stamp, verified_at=stamp)
            ids["v-bad"] = catalog.begin_replacement(
                file_id, object_key="vaults/x/notes.txt", provider_version_id="v-bad", size=4,
                storage_class="STANDARD", created_at="2026-07-22T10:00:00+00:00")
            # Recovery restores a file whose Local Copy is gone.
            catalog.mark_local_copy_missing(file_id, observed_at="2026-07-22T11:00:00+00:00")
            self.file_id = file_id
        return database_path, ids

    def test_list_versions_marks_non_selectable_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, ids = self._fixture(Path(directory))
            with PostgresConnection(str(database_path)) as connection:
                versions = ArchiveCatalog(connection).list_versions(2, "notes.txt")
                by_provider = {row["provider_version_id"]: row for row in versions}
                # A superseded version is never offered.
                self.assertEqual(set(by_provider), {"v-new", "v-bad"})
                self.assertFalse(by_provider["v-bad"]["recoverable"])
                self.assertEqual(by_provider["v-bad"]["not_selectable_reason"], "pending")
                self.assertTrue(by_provider["v-new"]["recoverable"])
                self.assertIsNone(by_provider["v-new"]["not_selectable_reason"])
                connection.execute(
                    "UPDATE archive_versions SET availability='missing', availability_checked_at=now() "
                    "WHERE id=%s", (ids["v-new"],))
                current = next(row for row in ArchiveCatalog(connection).list_versions(2, "notes.txt")
                               if row["role"] == "current")
                self.assertFalse(current["recoverable"])
                self.assertEqual(current["not_selectable_reason"], "missing")

    def test_superseded_available_copy_cannot_be_selected_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, ids = self._fixture(Path(directory))
            with PostgresConnection(str(database_path)) as connection:
                job_ids, _total, eligible = ArchiveCatalog(connection).queue_jobs(
                    vault_id=2,
                    path="notes.txt",
                    action="recover",
                    requested_at="2026-07-22T12:00:00+00:00",
                    group_id="00000000-0000-0000-0000-0000000000a1",
                    is_directory=False,
                    archive_version_id=ids["v-old"],
                )
            self.assertEqual(eligible, 0)
            self.assertEqual(job_ids, [])

    def test_default_recover_targets_the_verified_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path, ids = self._fixture(Path(directory))
            with PostgresConnection(str(database_path)) as connection:
                job_ids, _, _ = ArchiveCatalog(connection).queue_jobs(
                    vault_id=2,
                    path="notes.txt",
                    action="recover",
                    requested_at="2026-07-22T12:00:00+00:00",
                    group_id="00000000-0000-0000-0000-0000000000b1",
                    is_directory=False,
                )
                job = connection.execute(
                    "SELECT archive_version_id FROM jobs WHERE id=%s",
                    (job_ids[0],),
                ).fetchone()
            self.assertEqual(str(job["archive_version_id"]), ids["v-new"])


if __name__ == "__main__":
    unittest.main()
