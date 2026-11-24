"""Internal recovery pins target the current verified snapshot, never history.

On the rebuilt model (ADR-0035) a Vault File has Current, at most one
replacement in progress, and superseded versions awaiting exact cleanup.
Only a verified, available Current is recoverable; the others are listed
with the reason they cannot be selected, or not listed at all.
"""

from __future__ import annotations

import hashlib
import unittest

from app.services.archive_versions import ArchiveVersions
from app.services.file_listing import FileListing
from app.services.local_copies import LocalCopies
from app.services.job_admission import JobAdmission

from tests.catalog_support import fingerprint, protect
from tests.fixtures import FrostVaultTestCase, seed_vault


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class VersionSelectionTests(FrostVaultTestCase):
    def _fixture(self) -> dict[str, str]:
        """``v-old`` superseded, ``v-new`` Current, ``v-bad`` the replacement in progress."""
        with self.database.connect() as connection:
            seed_vault(connection, source_root=str(self.tmp_path / "source"), id=2)
            local_copies = LocalCopies(connection)
            file_id = local_copies.observe_local_copy(
                vault_id=2, path="notes.txt", file_type="regular", size=4, mtime_ns=1,
                observed_at="2026-07-20T09:00:00+00:00")
            ids = {}
            for name, payload, stamp in (("v-old", b"old!", "2026-07-20T10:00:00+00:00"),
                                         ("v-new", b"new!", "2026-07-21T10:00:00+00:00")):
                fingerprint(connection, 2, "notes.txt", _sha(payload))
                ids[name] = protect(connection, 2, "notes.txt", _sha(payload), provider_version_id=name,
                                    created_at=stamp, verified_at=stamp)
            ids["v-bad"] = ArchiveVersions(connection).begin_replacement(
                file_id, object_key="vaults/x/notes.txt", provider_version_id="v-bad", size=4,
                storage_class="STANDARD", created_at="2026-07-22T10:00:00+00:00")
            # Recovery restores a file whose Local Copy is gone.
            local_copies.mark_local_copy_missing(file_id, observed_at="2026-07-22T11:00:00+00:00")
            self.file_id = file_id
        return ids

    def test_list_versions_marks_non_selectable_with_reason(self) -> None:
        ids = self._fixture()
        with self.database.connect() as connection:
            versions = FileListing(connection).list_versions(2, "notes.txt")
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
            current = next(row for row in FileListing(connection).list_versions(2, "notes.txt")
                           if row["role"] == "current")
            self.assertFalse(current["recoverable"])
            self.assertEqual(current["not_selectable_reason"], "missing")

    def test_superseded_available_copy_cannot_be_selected_for_recovery(self) -> None:
        ids = self._fixture()
        with self.database.connect() as connection:
            job_ids, _total, eligible = JobAdmission(connection).queue_jobs(
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
        ids = self._fixture()
        with self.database.connect() as connection:
            job_ids, _, _ = JobAdmission(connection).queue_jobs(
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
