"""SQLite half of #417's populated upgrade/downgrade/re-upgrade contract."""
import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from tests.essential_file_actions_migration_contract import assert_file_actions_migration
from tests.test_database import run_alembic


class EssentialFileActionsMigrationTests(unittest.TestCase):
    def test_retains_cloud_content_identity_and_history_and_cancels_only_legacy_hides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            assert_file_actions_migration(
                self,
                lambda target, command="upgrade": run_alembic(path, target, command),
                lambda: SQLiteConnection(str(path)),
            )
