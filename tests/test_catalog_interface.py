"""ArchiveCatalog through one interface on the schema head.

Domain specification kept from the v0.5 suite (ADR-0035): #587 ports the
catalog to the rebuilt ``files`` table and makes these pass again.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.postgres import PostgresConnection, run_alembic


class CatalogInterfaceTests(unittest.TestCase):
    def test_catalog_observes_a_new_local_file_through_one_interface(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                    """
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
                    vault_id=2,
                    path="reports/annual.txt",
                    file_type="regular",
                    size=21,
                    mtime_ns=1_750_000_000_250_000_000,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                observed = catalog.get_file_by_path(2, "reports/annual.txt")

            self.assertEqual(observed["id"], file_id)
            self.assertEqual(observed["local_copy"]["presence"], "present")
            self.assertEqual(observed["local_copy"]["mtime_ns"], 1_750_000_000_250_000_000)
            self.assertIsNone(observed["latest_version"])

    def test_confirmed_rename_preserves_identity_and_path_history(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rename.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix
                    ) VALUES (2, 'photos', 'Photos', '/source', 'bucket', 'photos')
                    """
                )
                catalog = ArchiveCatalog(connection)
                file_id = catalog.observe_local_copy(
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
                renamed = catalog.get_file_by_path(
                    2, "test/fotodefinitiva.jpg"
                )
                old_path = catalog.get_file_by_path(2, "test/fototest.jpg")
                history = catalog.list_path_history(file_id, vault_id=2)

            self.assertEqual(renamed["id"], file_id)
            self.assertIsNone(old_path)
            self.assertEqual(
                [entry["path"] for entry in history],
                ["test/fototest.jpg", "test/fotodefinitiva.jpg"],
            )

    def test_stale_cloud_scan_does_not_hide_a_concurrent_upload(self) -> None:
        from app.catalog import ArchiveCatalog

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent-upload.db"
            migrated = run_alembic(path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with PostgresConnection(str(path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs')
                    """
                )
                catalog = ArchiveCatalog(connection)
                catalog.record_archive_version(
                    vault_id=2,
                    path="old.txt",
                    object_key="docs/old.txt",
                    provider_version_id="old-version",
                    size=1,
                    storage_class="STANDARD",
                    etag="old",
                    uploaded_at="2026-07-21T09:00:00+00:00",
                    observed_at="2026-07-21T09:00:00+00:00",
                    scan_id="2026-07-21T09:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="new.txt",
                    object_key="docs/new.txt",
                    provider_version_id="new-version",
                    size=1,
                    storage_class="STANDARD",
                    etag="new",
                    uploaded_at="2026-07-21T11:00:00+00:00",
                    observed_at="2026-07-21T11:00:00+00:00",
                    scan_id="2026-07-21T11:00:00+00:00",
                    origin="upload",
                )
                catalog.mark_unseen_archive_versions_missing(
                    vault_id=2,
                    scan_id="2026-07-21T10:00:00+00:00",
                    scan_started_at="2026-07-21T10:00:00+00:00",
                )
                old = catalog.list_versions(2, "old.txt")
                new = catalog.list_versions(2, "new.txt")

            self.assertEqual(old[0]["availability"], "missing")
            self.assertEqual(new[0]["availability"], "available")


if __name__ == "__main__":
    unittest.main()
