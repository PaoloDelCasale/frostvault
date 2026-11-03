"""The plaintext-only migration preserves populated state and rejects crypt."""
import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from tests.test_database import run_alembic
from tests.plaintext_vaults_migration_contract import assert_plaintext_migration


class PlaintextVaultMigrationTests(unittest.TestCase):
    def test_populated_upgrade_downgrade_reupgrade_and_legacy_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            assert_plaintext_migration(
                self, lambda target, command="upgrade": run_alembic(path, target, command),
                lambda: SQLiteConnection(str(path)),
            )
