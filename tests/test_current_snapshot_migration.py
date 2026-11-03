"""PostgreSQL half of the populated #418 migration contract."""
from tests.postgres import PostgresConnection, database_dsn
import tempfile
import unittest
from pathlib import Path

from tests.current_snapshot_migration_contract import assert_current_snapshot_migration
from tests.test_database import run_alembic


class CurrentSnapshotMigrationTests(unittest.TestCase):
    def test_preserves_every_cloud_copy_and_trust_on_upgrade_downgrade_reupgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.db'
            assert_current_snapshot_migration(
                self, lambda target, command='upgrade': run_alembic(path, target, command),
                lambda: PostgresConnection(str(path)),
            )
