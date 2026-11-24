"""ArchiveCatalog through one interface on the schema head.

Domain specification kept from the v0.5 suite (ADR-0035), ported by #587 to
the rebuilt ``files`` table: one row per Vault File with explicit Current and
replacement pointers.
"""
from __future__ import annotations

import unittest

from tests import catalog_probe
from tests.fixtures import fresh_database, seed_vault


class CatalogInterfaceTests(unittest.TestCase):
    def test_catalog_observes_a_new_local_file_through_one_interface(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.services.local_copies import LocalCopies

        with fresh_database() as database:
            with database.connect() as connection:
                seed_vault(connection, id=2, source_root="/source")
                catalog = ArchiveCatalog(connection)
                local_copies = LocalCopies(connection)
                file_id = local_copies.observe_local_copy(
                    vault_id=2,
                    path="reports/annual.txt",
                    file_type="regular",
                    size=21,
                    mtime_ns=1_750_000_000_250_000_000,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                observed = catalog_probe.get_file_by_path(catalog, 2, "reports/annual.txt")

            self.assertEqual(observed["id"], file_id)
            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(observed["local_copy"]["mtime_ns"], 1_750_000_000_250_000_000)
            self.assertIsNone(observed["current_version"])
            self.assertIsNone(observed["replacing_version"])

    def test_confirmed_rename_preserves_identity_and_path_history(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.services.local_copies import LocalCopies

        with fresh_database() as database:
            with database.connect() as connection:
                seed_vault(connection, name="Photos", id=2, source_root="/source")
                catalog = ArchiveCatalog(connection)
                local_copies = LocalCopies(connection)
                file_id = local_copies.observe_local_copy(
                    vault_id=2,
                    path="test/fototest.jpg",
                    file_type="regular",
                    size=21,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.rename_file(
                    file_id,
                    new_path="test/fotodefinitiva.jpg",
                    changed_at="2026-07-21T11:00:00+00:00",
                    vault_id=2,
                )
                renamed = catalog_probe.get_file_by_path(catalog, 
                    2, "test/fotodefinitiva.jpg"
                )
                old_path = catalog_probe.get_file_by_path(catalog, 2, "test/fototest.jpg")
                history = catalog_probe.list_path_history(catalog, file_id, vault_id=2)

            self.assertEqual(renamed["id"], file_id)
            self.assertIsNone(old_path)
            self.assertEqual(
                [entry["path"] for entry in history],
                ["test/fototest.jpg", "test/fotodefinitiva.jpg"],
            )

    def test_stale_cloud_scan_does_not_hide_a_concurrent_upload(self) -> None:
        from app.catalog import ArchiveCatalog
        from app.services.local_copies import LocalCopies

        with fresh_database() as database:
            with database.connect() as connection:
                seed_vault(connection, id=2, source_root="/source")
                catalog = ArchiveCatalog(connection)
                local_copies = LocalCopies(connection)
                ids = {}
                for name in ("old.txt", "new.txt"):
                    ids[name] = local_copies.observe_local_copy(
                        vault_id=2, path=name, file_type="regular", size=1,
                        mtime_ns=1, observed_at="2026-07-21T08:00:00+00:00",
                    )
                # The old version predates the scan; the new one is an upload
                # that completed while the scan was listing.
                catalog.begin_replacement(
                    ids["old.txt"],
                    object_key="vaults/x/old.txt",
                    provider_version_id="old-version",
                    size=1,
                    storage_class="STANDARD",
                    created_at="2026-07-21T09:00:00+00:00",
                )
                catalog.begin_replacement(
                    ids["new.txt"],
                    object_key="vaults/x/new.txt",
                    provider_version_id="new-version",
                    size=1,
                    storage_class="STANDARD",
                    created_at="2026-07-21T11:00:00+00:00",
                )
                missing = catalog.mark_unseen_archive_versions_missing(
                    vault_id=2,
                    scan_id="2026-07-21T10:00:00+00:00",
                    scan_started_at="2026-07-21T10:00:00+00:00",
                )
                old = catalog_probe.get_file_by_path(catalog, 2, "old.txt")
                new = catalog_probe.get_file_by_path(catalog, 2, "new.txt")

            self.assertEqual(old["replacing_version"]["availability"], "missing")
            self.assertEqual(new["replacing_version"]["availability"], "available")
            self.assertEqual([row["path"] for row in missing], ["old.txt"])


if __name__ == "__main__":
    unittest.main()
