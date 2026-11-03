from tests.postgres import PostgresConnection, database_dsn
import tempfile
import unittest
from pathlib import Path

from tests.test_database import run_alembic
from tests.storage_plan_migration_contract import assert_storage_plan_migration, assert_ambiguous_plan_refused


class StoragePlanMigrationTests(unittest.TestCase):
    def run_contract(self, contract, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            contract(self, lambda target, command="upgrade": run_alembic(path, target, command), lambda: PostgresConnection(str(path)), **kwargs)

    def test_populated_upgrade_downgrade_reupgrade_preserves_only_cloud_copy(self):
        self.run_contract(assert_storage_plan_migration)

    def test_heterogeneous_folder_policies_stop_before_ddl(self):
        self.run_contract(assert_ambiguous_plan_refused)

    def test_missing_default_and_redundant_pins_map_to_standard(self):
        from tests.storage_plan_safety_contract import assert_standard_plan_mapping
        self.run_contract(assert_standard_plan_mapping)

    def test_unsafe_legacy_namespaces_stop_before_ddl(self):
        from tests.storage_plan_safety_contract import assert_unsafe_namespaces_refused
        self.run_contract(assert_unsafe_namespaces_refused)

    def test_pins_on_transitioning_vault_stop_before_ddl(self):
        self.run_contract(assert_ambiguous_plan_refused, pinned=True)
