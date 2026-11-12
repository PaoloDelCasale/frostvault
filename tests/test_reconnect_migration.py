"""0058 populated upgrade/downgrade preserves sole cloud evidence (CI PostgreSQL)."""
from pathlib import Path
import tempfile
import unittest

from tests.historical_catalog_fixture import HistoricalCatalogFixture as ArchiveCatalog
from tests.postgres import PostgresConnection, run_alembic


class ReconnectMigrationTests(unittest.TestCase):
    def test_populated_upgrade_and_downgrade_have_no_cloud_or_catalog_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration"
            result = run_alembic(path, "0057_vault_local_release")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                connection.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (1, 'owner', 'Owner', 'hash')")
                connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES(1,'photos','Photos','/sources/photos/root','bucket','vaults/one')")
                catalog = ArchiveCatalog(connection)
                version = catalog.record_archive_version(vault_id=1, path="photo.jpg", object_key="vaults/one/photo.jpg", provider_version_id="sole-copy", size=5,
                    storage_class="DEEP_ARCHIVE", etag="etag", uploaded_at="2026-11-02", observed_at="2026-11-02", scan_id="old")
                catalog.mark_version_verified(version, plaintext_sha256="a" * 64, verified_at="2026-11-02")
                before = connection.execute("SELECT * FROM archive_versions").fetchall()
            result = run_alembic(path, "0058_reconnect_recovery_kit")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
                self.assertIsNone(connection.execute("SELECT reconnect_volume_fingerprint FROM vaults").fetchone()["reconnect_volume_fingerprint"])
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM recovery_kit_custody").fetchone()["n"], 0)
            result = run_alembic(path, "0057_vault_local_release", "downgrade")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
            result = run_alembic(path, "0058_reconnect_recovery_kit")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(path) as connection:
                connection.execute("UPDATE vaults SET reconnect_volume_fingerprint='accepted-mount'")
            result = run_alembic(path, "0057_vault_local_release", "downgrade")
            self.assertNotEqual(result.returncode, 0)
            with PostgresConnection(path) as connection:
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0058_reconnect_recovery_kit")
                self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
