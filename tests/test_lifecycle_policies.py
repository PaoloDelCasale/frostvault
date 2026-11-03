import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.lifecycle_policies import load_vault_plan, set_vault_plan, sync_lifecycle_rules_for_bucket
from tests.test_database import run_alembic


class VaultStoragePlanTests(unittest.TestCase):
    def test_isolated_vault_plans_preserve_catalog_and_only_cloud_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with SQLiteConnection(str(path)) as connection:
                for i in [1, 2]:
                    connection.execute("INSERT INTO vaults(id, uuid, slug, name, source_root, s3_bucket, s3_prefix) VALUES (%s, %s, %s, 'Vault', '/source', 'bucket', %s)", (i, f"uuid-{i}", f"v-{i}", f"vaults/uuid-{i}/"))
                catalog = ArchiveCatalog(connection)
                version = catalog.record_archive_version(vault_id=1, path="only.txt", object_key="vaults/uuid-1/only.txt", provider_version_id="only-copy", size=5, storage_class="STANDARD", etag="etag", uploaded_at="2026-01-01", observed_at="2026-01-01", scan_id="scan")
                catalog.mark_version_verified(version, plaintext_sha256="a" * 64, verified_at="2026-01-01")
                before = connection.execute("SELECT * FROM archive_versions").fetchall()
                for preset in ["archive_tiered", "deep_archive_early", "standard_only"]:
                    set_vault_plan(connection, 1, GUIDED_PROFILES[preset])
                    self.assertEqual(load_vault_plan(connection, 1), GUIDED_PROFILES[preset])
                    self.assertEqual(load_vault_plan(connection, 2), GUIDED_PROFILES["standard_only"])
                    self.assertEqual(connection.execute("SELECT * FROM archive_versions").fetchall(), before)
                    client = Mock()
                    client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
                    count = sync_lifecycle_rules_for_bucket(connection, client, bucket="bucket")
                    self.assertEqual(count, int(preset != "standard_only"))
                    client.delete_object.assert_not_called()
                    client.delete_objects.assert_not_called()
                    client.copy_object.assert_not_called()
                    client.put_object_tagging.assert_not_called()
