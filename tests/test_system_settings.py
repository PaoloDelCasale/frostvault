from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.services import metadata_backups
from app.sessions import create_session, csrf_token_for
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

    def test_precedence_database_over_environment_and_builtin(self) -> None:
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

    def _assert_setting_rejected(self, key: str, value: object) -> None:
        with SQLiteConnection(str(self.database_path)) as connection:
            with self.assertRaises(InvalidSystemSetting):
                set_system_setting(
                    connection,
                    key=key,
                    value=value,
                    updated_by=self.admin_id,
                )

    def test_unknown_keys_are_rejected(self) -> None:
        self._assert_setting_rejected("future_environment_key", "unsafe")

    def test_deployment_only_keys_are_rejected(self) -> None:
        self._assert_setting_rejected("archive_master_key", "unsafe")

    def test_incorrectly_typed_values_are_rejected(self) -> None:
        self._assert_setting_rejected("scan_interval", "1800")
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
            oidc_settings_encryption_key=(
                "oidc-settings-key-that-must-not-leak"
            ),
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
        self.assertNotIn("oidc_settings_encryption_key", snapshot)
        self.assertNotIn(
            "oidc-settings-key-that-must-not-leak",
            str(snapshot),
        )


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
            oidc_settings_encryption_key=(
                "oidc-settings-key-that-must-not-leak"
            ),
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

    def _authenticate(self, user_id: int) -> str:
        with SQLiteConnection(str(self.database_path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf_token = csrf_token_for(connection, token)
        self.client.cookies.set(self.test_settings.session_cookie_name, token)
        assert csrf_token is not None
        return csrf_token

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
        self.assertNotIn(
            "oidc-settings-key-that-must-not-leak",
            response.text,
        )
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

    def test_reauthenticated_admin_applies_audited_runtime_override(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings")
        self.assertEqual(before.status_code, 200, before.text)

        updated = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before.json()["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        )

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["revision"], before.json()["revision"] + 1)
        scan_interval = next(
            item
            for item in updated.json()["groups"]["operations"]
            if item["key"] == "scan_interval"
        )
        self.assertEqual(scan_interval["effective_value"], 1800)
        self.assertEqual(scan_interval["source"], "database_override")

        persisted = self.client.get("/api/admin/settings")
        self.assertEqual(persisted.json(), updated.json())
        events = self.client.get("/api/admin/audit-events").json()["events"]
        event = next(
            item for item in events if item["event"] == "system_settings.updated"
        )
        self.assertEqual(event["actor_user_id"], self.admin_id)
        self.assertEqual(
            event["detail"]["changes"],
            [
                {
                    "key": "scan_interval",
                    "old_value": 21600,
                    "new_value": 1800,
                    "old_source": "built_in_default",
                    "new_source": "database_override",
                }
            ],
        )

    def test_deployment_only_setting_returns_actionable_422_without_changes(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"archive_master_key": "must-not-be-persisted"},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("archive_master_key", response.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_mutation_requires_recent_reauthentication(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                ("2000-01-01T00:00:00+00:00", self.admin_id),
            )

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})

    def test_out_of_bounds_value_returns_actionable_422_without_changes(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"operation_concurrency": 17},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("operation_concurrency", response.json()["detail"])
        self.assertIn("16", response.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_security_duration_below_safe_minimum_is_rejected(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"invite_ttl_seconds": 299},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("invite_ttl_seconds", response.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_metadata_backup_prefix_rejects_path_traversal(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"metadata_backup_s3_prefix": "../secrets/"},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("metadata_backup_s3_prefix", response.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_conflicting_metadata_backup_intervals_are_rejected_atomically(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {
                    "metadata_backup_interval_seconds": 604800,
                    "metadata_backup_verify_interval_seconds": 86400,
                },
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("metadata_backup_verify_interval_seconds", response.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_removing_override_restores_default_precedence(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()
        overridden = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        ).json()

        restored = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": overridden["revision"],
                "overrides": {},
                "removals": ["scan_interval"],
            },
        )

        self.assertEqual(restored.status_code, 200, restored.text)
        scan_interval = next(
            item
            for item in restored.json()["groups"]["operations"]
            if item["key"] == "scan_interval"
        )
        self.assertEqual(scan_interval["effective_value"], 21600)
        self.assertEqual(scan_interval["source"], "built_in_default")

    def test_stale_revision_cannot_overwrite_newer_override(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()
        first = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        )
        self.assertEqual(first.status_code, 200, first.text)

        stale = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"scan_interval": 3600},
                "removals": [],
            },
        )

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"], "stale_system_settings")
        self.assertEqual(stale.json()["current_revision"], first.json()["revision"])
        current = self.client.get("/api/admin/settings").json()
        scan_interval = next(
            item
            for item in current["groups"]["operations"]
            if item["key"] == "scan_interval"
        )
        self.assertEqual(scan_interval["effective_value"], 1800)

    def test_audit_failure_rolls_back_the_override(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_system_settings_audit
                BEFORE INSERT ON audit_events
                WHEN NEW.event = 'system_settings.updated'
                BEGIN
                    SELECT RAISE(ABORT, 'audit unavailable');
                END
                """
            )
        failing_client = TestClient(
            main.app,
            client=("127.0.0.1", 50000),
            raise_server_exceptions=False,
        )
        failing_client.cookies.update(self.client.cookies)

        response = failing_client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.client.get("/api/admin/settings").json(), before)

    def test_updated_reauthentication_window_is_used_immediately(self) -> None:
        csrf_token = self._authenticate(self.admin_id)
        before = self.client.get("/api/admin/settings").json()
        updated = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": before["revision"],
                "overrides": {"reauth_window_seconds": 60},
                "removals": [],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        stale_reauth = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                (stale_reauth, self.admin_id),
            )

        response = self.client.patch(
            "/api/admin/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "revision": updated.json()["revision"],
                "overrides": {"scan_interval": 1800},
                "removals": [],
            },
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})


if __name__ == "__main__":
    unittest.main()
