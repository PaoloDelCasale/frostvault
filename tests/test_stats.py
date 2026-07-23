from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.main import stats
from tests.test_database import run_alembic


class StatsTests(unittest.TestCase):
    def test_stats_read_local_and_cloud_totals_from_versioned_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            migrated = run_alembic(database_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            with SQLiteConnection(str(database_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO vaults(
                        id, slug, name, source_root, s3_bucket, s3_prefix,
                        rclone_remote
                    ) VALUES (2, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')
                    """
                )
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(
                    vault_id=2,
                    path="report.txt",
                    file_type="regular",
                    size=12,
                    mtime_ns=10,
                    observed_at="2026-07-21T10:00:00+00:00",
                )
                catalog.record_archive_version(
                    vault_id=2,
                    path="report.txt",
                    object_key="docs/report.txt",
                    provider_version_id="version-1",
                    size=44,
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at="2026-07-21T10:00:00+00:00",
                    observed_at="2026-07-21T10:00:00+00:00",
                    scan_id="2026-07-21T10:00:00+00:00",
                )
            test_settings = SimpleNamespace(
                db_backend="sqlite",
                sqlite_path=str(database_path),
                allow_local_delete=False,
            )
            with (
                patch("app.database.settings", test_settings),
                patch("app.main.settings", test_settings),
            ):
                result = stats({"id": 2, "role": "viewer"})

            self.assertEqual(result["states"], {"both": 1})
            self.assertEqual(result["storage"], {"local_bytes": 12, "cloud_bytes": 44})

if __name__ == "__main__":
    unittest.main()
