from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.services import metadata_backups
from app.sessions import create_session
from app.system_settings import (
    InvalidSystemSetting,
    resolve_system_settings,
    set_system_setting,
)
from tests.test_database import run_alembic


class SystemSettingsResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', 'hash', TRUE) RETURNING id
                """
            ).fetchone()["id"]

    def test_database_override_precedes_environment_and_builtin_defaults(self) -> None:
        configured = replace(settings, scan_interval=7200, restore_days=5)
        with SQLiteConnection(str(self.database_path)) as connection:
            set_system_setting(
                connection,
                key="scan_interval",
                value=1800,
                updated_by=self.admin_id,
            )
            set_system_setting(
                connection,
                key="restore_tier",
                value="Standard",
                updated_by=self.admin_id,
            )
            resolved = resolve_system_settings(
                connection,
                settings_obj=configured,
                environ={
                    "SCAN_INTERVAL_SECONDS": "7200",
                    "RESTORE_DAYS": "5",
                },
            )

        self.assertEqual(resolved["scan_interval"].value, 1800)
        self.assertEqual(resolved["scan_interval"].source, "database_override")
        self.assertEqual(resolved["restore_tier"].value, "Standard")
        self.assertEqual(resolved["restore_days"].value, 5)
        self.assertEqual(resolved["restore_days"].source, "environment_default")

    def test_unknown_deployment_only_and_incorrectly_typed_values_are_rejected(self) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            for key, value in (
                ("future_environment_key", "unsafe"),
                ("archive_master_key", "unsafe"),
                ("scan_interval", "1800"),
            ):
                with self.subTest(key=key), self.assertRaises(InvalidSystemSetting):
                    set_system_setting(
                        connection,
                        key=key,
                        value=value,
                        updated_by=self.admin_id,
                    )

        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO system_settings(key, value, updated_by, updated_at)
                    VALUES ('scan_interval', '"not-an-integer"', %s, '2026-07-27')
                    """,
                    (self.admin_id,),
                )

    def test_metadata_snapshot_contains_effective_non_secret_settings_only(self) -> None:
        configured = replace(
            settings,
            archive_master_key="archive-secret-that-must-not-leak",
        )
        with SQLiteConnection(str(self.database_path)) as connection:
            set_system_setting(
                connection,
                key="scan_interval",
                value=1800,
                updated_by=self.admin_id,
            )
            snapshot = metadata_backups.build_config_snapshot(
                configured,
                connection=connection,
            )

        self.assertEqual(snapshot["scan_interval"], 1800)
        self.assertNotIn("archive_master_key", snapshot)
        self.assertNotIn("archive-secret-that-must-not-leak", str(snapshot))


class SystemSettingsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = self._create_user(connection, "admin", is_admin=True)
            self.member_id = self._create_user(connection, "member")

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            archive_master_key="archive-secret-that-must-not-leak",
            oidc_client_secret="oidc-secret-that-must-not-leak",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    @staticmethod
    def _create_user(connection, username: str, *, is_admin: bool = False) -> int:
        return connection.execute(
            """
            INSERT INTO users(username, display_name, password_hash, is_admin)
            VALUES (%s, %s, 'hash', %s) RETURNING id
            """,
            (username, username.title(), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
        self.client.cookies.set(self.test_settings.session_cookie_name, token)

    def test_admin_receives_grouped_effective_settings_with_secrets_redacted(self) -> None:
        self._authenticate(self.admin_id)
        response = self.client.get("/api/admin/settings")

        self.assertEqual(response.status_code, 200, response.text)
        groups = response.json()["groups"]
        self.assertEqual(
            set(groups),
            {"security", "oidc", "operations", "restore", "vault_defaults"},
        )
        self.assertNotIn("archive-secret-that-must-not-leak", response.text)
        self.assertNotIn("oidc-secret-that-must-not-leak", response.text)
        archive_key = next(
            item for item in groups["security"] if item["key"] == "archive_master_key"
        )
        self.assertEqual(archive_key["configured"], True)
        self.assertNotIn("effective_value", archive_key)
        scan_interval = next(
            item for item in groups["operations"] if item["key"] == "scan_interval"
        )
        self.assertIn("effective_value", scan_interval)
        self.assertIn("source", scan_interval)
        self.assertIn("mutability", scan_interval)
        self.assertIn("restart_required", scan_interval)

    def test_non_admin_receives_forbidden(self) -> None:
        self._authenticate(self.member_id)
        response = self.client.get("/api/admin/settings")
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
