"""Tests for automatic schema migration on application start."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alembic.script.revision import RangeNotAncestorError
from cryptography.fernet import Fernet

from app.config import settings as configured_settings
from app.database import HEAD_SCHEMA_REVISION, SQLiteConnection, initialize_database
from app.migrate_on_start import (
    SchemaMigrationError,
    _upgrade_existing_with_backup,
    ensure_schema_current,
)
from app.services import metadata_backups
from tests.test_database import run_alembic


class MigrateOnStartTests(unittest.TestCase):
    def _seed_backup_admin(self, path: Path) -> int:
        with SQLiteConnection(str(path)) as connection:
            return connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('backup-admin', 'Backup Admin', 'hash', TRUE)
                RETURNING id
                """
            ).fetchone()["id"]

    def _assert_durable_pre_upgrade_failure(
        self,
        path: Path,
        *,
        admin_id: int,
        block_reason: metadata_backups.PreUpgradeBackupBlockReason,
    ) -> None:
        expected_message = metadata_backups.pre_upgrade_backup_failure_message(
            block_reason
        )
        with SQLiteConnection(str(path)) as connection:
            run = connection.execute(
                """
                SELECT reason, status, error_message
                FROM metadata_backup_runs
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            run_count = connection.execute(
                "SELECT COUNT(*) AS total FROM metadata_backup_runs"
            ).fetchone()["total"]
            notification = connection.execute(
                """
                SELECT event, body FROM notifications
                WHERE user_id=%s
                ORDER BY id DESC LIMIT 1
                """,
                (admin_id,),
            ).fetchone()
            notification_count = connection.execute(
                "SELECT COUNT(*) AS total FROM notifications WHERE user_id=%s",
                (admin_id,),
            ).fetchone()["total"]

        self.assertEqual(run_count, 1)
        self.assertEqual(notification_count, 1)
        self.assertEqual(run["reason"], "pre_upgrade")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_message"], expected_message)
        self.assertEqual(notification["event"], "metadata_backup_failed")
        self.assertIn(expected_message, notification["body"])

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
                derived_tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertEqual(revision, HEAD_SCHEMA_REVISION)
            self.assertIn("vault_catalog_revisions", derived_tables)
            self.assertIn("filesystem_health_snapshots", derived_tables)

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

    def test_unavailable_store_failure_is_durable_after_startup_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unavailable-store.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            admin_id = self._seed_backup_admin(path)
            test_settings = replace(
                configured_settings,
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
                metadata_backup_dir=str(Path(directory) / "backups"),
                metadata_backup_retention=14,
                vault_s3_bucket="production-backups",
                archive_master_key=Fernet.generate_key().decode("ascii"),
            )

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.metadata_backups.default_object_store",
                    side_effect=metadata_backups.ObjectStoreUnavailableError(
                        "Configured metadata backup object store is unavailable"
                    ),
                ),
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "unavailable"):
                    _upgrade_existing_with_backup()
            upgrade.assert_not_called()
            self._assert_durable_pre_upgrade_failure(
                path,
                admin_id=admin_id,
                block_reason=(
                    metadata_backups.PreUpgradeBackupBlockReason.OFF_HOST_UNAVAILABLE
                ),
            )

    def test_missing_key_failure_is_durable_after_startup_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-key.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            admin_id = self._seed_backup_admin(path)
            test_settings = replace(
                configured_settings,
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
                metadata_backup_dir=str(Path(directory) / "backups"),
                metadata_backup_retention=14,
                vault_s3_bucket="production-backups",
                archive_master_key="",
            )

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.metadata_backups.default_object_store",
                    return_value=object(),
                ),
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "ARCHIVE_MASTER_KEY"):
                    _upgrade_existing_with_backup()
            upgrade.assert_not_called()
            self._assert_durable_pre_upgrade_failure(
                path,
                admin_id=admin_id,
                block_reason=(
                    metadata_backups.PreUpgradeBackupBlockReason.MASTER_KEY_REQUIRED
                ),
            )

    def test_local_only_missing_key_is_explicit_and_still_upgrades(self) -> None:
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
                archive_master_key="",
            )

            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    side_effect=["0021_local_retention", HEAD_SCHEMA_REVISION],
                ),
                patch(
                    "app.migrate_on_start.metadata_backups.run_pre_upgrade_backup"
                ) as backup,
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                self.assertEqual(ensure_schema_current(), "upgraded")
            backup.assert_not_called()
            upgrade.assert_called_once_with()

    def test_db_ahead_of_build_blocks_before_backup_or_alembic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ahead.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
            )

            def iterate(upper: str, lower: str):
                if (upper, lower) == (HEAD_SCHEMA_REVISION, "future_revision"):
                    raise RangeNotAncestorError(lower, upper)
                self.assertEqual((upper, lower), ("future_revision", HEAD_SCHEMA_REVISION))
                return iter(())

            scripts = SimpleNamespace(
                get_heads=lambda: [HEAD_SCHEMA_REVISION],
                get_revision=lambda revision: SimpleNamespace(revision=revision),
                iterate_revisions=iterate,
            )
            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    return_value="future_revision",
                ),
                patch(
                    "app.migrate_on_start._alembic_script_directory",
                    return_value=scripts,
                ),
                patch("app.migrate_on_start._upgrade_existing_with_backup") as backup,
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "ahead"):
                    ensure_schema_current()
            backup.assert_not_called()
            upgrade.assert_not_called()

    def test_unknown_db_revision_blocks_before_backup_or_alembic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.db"
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
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    return_value="not-in-this-build",
                ),
                patch("app.migrate_on_start._upgrade_existing_with_backup") as backup,
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "unknown"):
                    ensure_schema_current()
            backup.assert_not_called()
            upgrade.assert_not_called()
            with SQLiteConnection(str(path)) as connection:
                records = connection.execute(
                    "SELECT COUNT(*) AS total FROM metadata_backup_runs"
                ).fetchone()["total"]
            self.assertEqual(records, 0)

    def test_divergent_alembic_graph_blocks_before_backup_or_alembic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "divergent.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
            )
            scripts = SimpleNamespace(
                get_heads=lambda: [HEAD_SCHEMA_REVISION, "other_branch_head"],
            )
            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch(
                    "app.migrate_on_start.read_schema_revision",
                    return_value="0021_local_retention",
                ),
                patch(
                    "app.migrate_on_start._alembic_script_directory",
                    return_value=scripts,
                ),
                patch("app.migrate_on_start._upgrade_existing_with_backup") as backup,
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "divergent"):
                    ensure_schema_current()
            backup.assert_not_called()
            upgrade.assert_not_called()

    def test_multiple_database_revisions_are_divergent_before_backup_or_alembic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multiple-heads.db"
            result = run_alembic(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with SQLiteConnection(str(path)) as connection:
                connection.execute(
                    "INSERT INTO alembic_version(version_num) VALUES (%s)",
                    ("other_branch_head",),
                )
            test_settings = SimpleNamespace(
                auto_migrate=True,
                db_backend="sqlite",
                sqlite_path=str(path),
            )
            with (
                patch("app.migrate_on_start.settings", test_settings),
                patch("app.database.settings", test_settings),
                patch("app.migrate_on_start._upgrade_existing_with_backup") as backup,
                patch("app.migrate_on_start._alembic_upgrade") as upgrade,
            ):
                with self.assertRaisesRegex(SchemaMigrationError, "divergent"):
                    ensure_schema_current()
            backup.assert_not_called()
            upgrade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
