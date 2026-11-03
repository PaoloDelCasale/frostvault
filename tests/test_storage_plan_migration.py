import tempfile
import unittest
from pathlib import Path
from app.database import SQLiteConnection
from tests.test_database import run_alembic
from tests.storage_plan_migration_contract import assert_storage_plan_migration, assert_ambiguous_plan_refused


class StoragePlanMigrationTests(unittest.TestCase):
    def run_contract(self, contract, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            contract(self, lambda target, command="upgrade": run_alembic(path, target, command), lambda: SQLiteConnection(str(path)), **kwargs)

    def test_populated_upgrade_downgrade_reupgrade_preserves_only_cloud_copy(self):
        self.run_contract(assert_storage_plan_migration)

    def test_heterogeneous_folder_policies_stop_before_ddl(self):
        self.run_contract(assert_ambiguous_plan_refused)

    def test_pins_on_transitioning_vault_stop_before_ddl(self):
        self.run_contract(assert_ambiguous_plan_refused, pinned=True)
