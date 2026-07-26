"""Tests for automatic schema migration on application start."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import HEAD_SCHEMA_REVISION, SQLiteConnection, initialize_database
from app.migrate_on_start import SchemaMigrationError, ensure_schema_current
from tests.test_database import run_alembic


class MigrateOnStartTests(unittest.TestCase):
    def test_disabled_auto_migrate_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "stale.db")
            test_settings = SimpleNamespace(
                auto_migrate=False,
                db_backend="sqlite",
                sqlite_path=path,
            )
            with patch("app.migrate_on_start.settings", test_settings):
                self.assertEqual(ensure_schema_current(), "disabled")

    def test_current_schema_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
            )
            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                self.assertEqual(ensure_schema_current(), "current")
                upgrade.assert_not_called()

    def test_fresh_database_bootstraps_via_alembic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fresh.db")
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=path,
                bootstrap_admin_username="admin",
                bootstrap_admin_password="a-secure-test-password",
                bootstrap_admin_display_name="Administrator",
                bootstrap_vault_slug="",
            )

            def fake_upgrade(revision: str = "head") -> None:
                completed = run_alembic(Path(path), revision=revision)
                if completed.returncode != 0:
                    raise SchemaMigrationError(completed.stderr)

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch("app.migrate_on_start._alembic_upgrade", side_effect=fake_upgrade),
            ):
                self.assertEqual(ensure_schema_current(), "bootstrapped")
                initialize_database()

            with SQLiteConnection(path) as connection:
                revision = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()["version_num"]
            self.assertEqual(revision, HEAD_SCHEMA_REVISION)

    def test_stale_schema_uses_backup_then_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.db"
            # Migrate to head, then pretend we are still on an older revision.
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)

            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
                metadata_backup_dir=str(Path(directory) / "backups"),
                metadata_backup_retention=14,
            )
            calls: list[str] = []

            def fake_upgrade_existing() -> None:
                calls.append("backup_upgrade")

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    side_effect=["0021_local_retention", HEAD_SCHEMA_REVISION],
                ),
                patch(
                    "app.migrate_on_start._upgrade_existing_with_backup",
                    side_effect=fake_upgrade_existing,
                ),
            ):
                self.assertEqual(ensure_schema_current(), "upgraded")
            self.assertEqual(calls, ["backup_upgrade"])

    def test_backup_failure_with_real_config_blocks_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
                metadata_backup_dir=str(Path(directory) / "backups"),
                metadata_backup_retention=14,
                vault_s3_bucket="",
                archive_master_key="",
            )

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    return_value="0021_local_retention",
                ),
                patch(
                    "app.migrate_on_start.metadata_backups.run_pre_upgrade_backup",
                    side_effect=__import__(
                        "app.services.metadata_backups", fromlist=["BackupError"]
                    ).BackupError("disk full writing backup"),
                ),
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaises(SchemaMigrationError) as ctx:
                    ensure_schema_current()
            self.assertIn("blocked", str(ctx.exception).lower())
            upgrade.assert_not_called()

    def test_missing_master_key_warns_and_still_upgrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dev.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
                metadata_backup_dir=str(Path(directory) / "backups"),
                metadata_backup_retention=14,
                vault_s3_bucket="",
            )
            BackupError = __import__(
                "app.services.metadata_backups", fromlist=["BackupError"]
            ).BackupError

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    side_effect=["0021_local_retention", HEAD_SCHEMA_REVISION],
                ),
                patch(
                    "app.migrate_on_start.metadata_backups.run_pre_upgrade_backup",
                    side_effect=BackupError("ARCHIVE_MASTER_KEY is not configured"),
                ),
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                self.assertEqual(ensure_schema_current(), "upgraded")
            upgrade.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
