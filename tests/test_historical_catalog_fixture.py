"""Historical seeds must not become an application compatibility path."""
import tempfile
import unittest
from pathlib import Path

from app.database import HEAD_SCHEMA_REVISION
from tests.historical_catalog_fixture import HistoricalCatalogFixture
from tests.postgres import PostgresConnection, run_alembic


class HistoricalCatalogFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "historical"

    def test_seed_builder_refuses_current_schema_without_mutation(self):
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(self.path) as connection:
            with self.assertRaisesRegex(ValueError, "requires the pre-0075 schema"):
                HistoricalCatalogFixture(connection)
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], HEAD_SCHEMA_REVISION)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM vault_files").fetchone()["n"], 0)

    def test_populated_historical_seed_is_not_adopted_and_failed_upgrade_preserves_evidence(self):
        result = run_alembic(self.path, "0072_upload_lineage_indexes")
        self.assertEqual(result.returncode, 0, result.stderr)
        tables = ("users", "vaults", "vault_files", "file_paths", "local_copies", "archive_versions")
        with PostgresConnection(self.path) as connection:
            connection.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','fixture-hash')")
            connection.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'test','Test','/not-a-real-source','fixture-bucket','test')")
            seed = HistoricalCatalogFixture(connection)
            file_id = seed.observe_local_copy(vault_id=2, path="sole.txt", file_type="regular", size=5,
                                              mtime_ns=1, observed_at="2026-11-06")
            version = seed.record_archive_version(vault_id=2, path="sole.txt", object_key="test/sole.txt",
                provider_version_id="exact-only-copy", size=5, storage_class="STANDARD", etag="not-proof",
                uploaded_at="2026-11-06", observed_at="2026-11-06", scan_id="fixture")
            seed.mark_version_verified(version, plaintext_sha256="a" * 64, verified_at="2026-11-06")
            seed.set_local_fingerprint(vault_id=2, path="sole.txt", plaintext_sha256="a" * 64,
                                       matched_archive_version_id=version)
            seed.mark_local_copy_missing(file_id, observed_at="2026-11-07")
            before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}
        result = run_alembic(self.path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an empty catalog", result.stderr)
        with PostgresConnection(self.path) as connection:
            self.assertEqual({table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}, before)
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0072_upload_lineage_indexes")
            self.assertIsNone(connection.execute("SELECT to_regclass('owner') AS name").fetchone()["name"])
