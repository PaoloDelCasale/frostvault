from tests.postgres import PostgresConnection, database_dsn
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from app.catalog import ArchiveCatalog

from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.lifecycle_policies import load_vault_plan, set_vault_plan, sync_lifecycle_rules_for_bucket
from tests.catalog_support import fingerprint, protect
from tests.owner_fixtures import insert_owner, insert_vault
from tests.test_database import run_alembic


class VaultStoragePlanTests(unittest.TestCase):
    def test_isolated_vault_plans_preserve_catalog_and_only_cloud_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with PostgresConnection(str(path)) as connection:
                insert_owner(connection)
                for i in [1, 2]:
                    insert_vault(connection, id=i, uuid=f"00000000-0000-4000-8000-00000000000{i}", name=f"Vault {i}", source_root=f"/source-{i}")
                catalog = ArchiveCatalog(connection)
                catalog.observe_local_copy(vault_id=1, path="only.txt", file_type="regular", size=5, mtime_ns=1, observed_at="2026-01-01T00:00:00+00:00")
                fingerprint(catalog, 1, "only.txt", "a" * 64)
                protect(catalog, 1, "only.txt", "a" * 64, created_at="2026-01-01T00:00:00+00:00", verified_at="2026-01-01T00:01:00+00:00")
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
