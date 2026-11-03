"""Targeted PostgreSQL migration proof; PostgreSQL runs the same contract in CI."""
from tests.postgres import PostgresConnection, database_dsn
import tempfile
import unittest
from pathlib import Path


from tests.test_database import run_alembic
from tests.s3_sdk_migration_contract import assert_sdk_migration


class SDKMigrationTests(unittest.TestCase):
    def test_populated_upgrade_downgrade_reupgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            assert_sdk_migration(self,
                lambda target, command="upgrade": run_alembic(path, target, command),
                lambda: PostgresConnection(str(path)))
