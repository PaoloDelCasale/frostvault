"""Populated PostgreSQL upgrade contract for #423; no cloud calls."""
import tempfile
import unittest
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.services.operation_policies import get_policy
from tests.postgres import PostgresConnection, run_alembic


class WatcherUploadMigrationTests(unittest.TestCase):
    def test_existing_pauses_and_only_cloud_copy_survive_upgrade_and_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "upgrade"
            result = run_alembic(identity, "0053_vault_storage_plans")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(identity) as connection:
                for vault_id in (1, 2, 3):
                    connection.execute("""
                        INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix)
                        VALUES (%s, %s, 'Photos', '/source', 'bucket', %s)
                    """, (vault_id, str(vault_id), str(vault_id)))
                connection.execute("INSERT INTO vault_operation_policies(vault_id, auto_upload) VALUES (2, FALSE), (3, TRUE)")
                catalog = ArchiveCatalog(connection)
                version = catalog.record_archive_version(
                    vault_id=1, path="sole.jpg", object_key="1/sole.jpg", provider_version_id="only-copy",
                    size=5, storage_class="STANDARD", etag="etag", uploaded_at="2026-11-03",
                    observed_at="2026-11-03", scan_id="legacy",
                )
                catalog.mark_version_verified(version, plaintext_sha256="a" * 64, verified_at="2026-11-03")
                before = connection.execute("SELECT * FROM archive_versions").fetchall()
            result = run_alembic(identity)
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(identity) as connection:
                self.assertFalse(get_policy(connection, 1).auto_upload)  # old implicit pause
                self.assertFalse(get_policy(connection, 2).auto_upload)  # old explicit pause
                self.assertTrue(get_policy(connection, 3).auto_upload)
                connection.execute("""
                    INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix)
                    VALUES (4, 'new', 'New', '/new', 'bucket', 'new')
                """)
                self.assertTrue(get_policy(connection, 4).auto_upload)
                connection.execute("INSERT INTO vault_operation_policies(vault_id) VALUES (4)")
                self.assertTrue(get_policy(connection, 4).auto_upload)  # database default too
                self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 0)
            result = run_alembic(identity, "0053_vault_storage_plans", "downgrade")
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(identity) as connection:
                self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
                self.assertFalse(connection.execute("SELECT auto_upload FROM vault_operation_policies WHERE vault_id=1").fetchone()["auto_upload"])
