from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from tests.test_database import run_alembic


class VaultLifecycleHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        migrated = run_alembic(self.path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            self.owner_id = self._user(connection, "owner", False)
            self.operator_id = self._user(connection, "operator", False)
            self.viewer_id = self._user(connection, "viewer", False)
            self.vault_id = connection.execute(
                "INSERT INTO vaults(slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES ('docs', 'Docs', '/source', 'bucket', 'docs', 'remote') RETURNING id"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (self.vault_id, self.owner_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'operator')",
                (self.vault_id, self.operator_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'viewer')",
                (self.vault_id, self.viewer_id),
            )
        test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.path),
            cookie_secure=False,
        )
        self.patchers = [
            patch("app.main.settings", test_settings),
            patch("app.database.settings", test_settings),
            patch("app.sessions.settings", test_settings),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    @staticmethod
    def _user(connection, name: str, is_admin: bool) -> int:
        return connection.execute(
            "INSERT INTO users(username, display_name, password_hash, is_admin) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name, name.title(), hash_password("a secure test password"), is_admin),
        ).fetchone()["id"]

    def _authenticate(self, user_id: int, *, reauth: bool = False) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            token = create_session(connection, user_id=user_id, auth_method="oidc")
            csrf = csrf_token_for(connection, token)
            if reauth:
                connection.execute(
                    "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                    (datetime.now(timezone.utc).isoformat(), user_id),
                )
        self.client.cookies.set(settings.session_cookie_name, token)
        self.client.cookies.set(settings.csrf_cookie_name, csrf)

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get(settings.csrf_cookie_name) or ""}

    def test_owner_can_read_lifecycle_catalog(self) -> None:
        self._authenticate(self.owner_id)
        response = self.client.get("/api/vault/lifecycle")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("ia_after_30", body["guided_profiles"])
        self.assertEqual(body["policies"], [])

    def test_nonowners_cannot_read_lifecycle(self) -> None:
        for user_id in (self.operator_id, self.viewer_id):
            with self.subTest(user_id=user_id):
                self._authenticate(user_id)
                response = self.client.get("/api/vault/lifecycle")
                self.assertEqual(response.status_code, 403)

    def test_owner_update_requires_reauth_and_applies_guided_profile(self) -> None:
        self._authenticate(self.owner_id)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s WHERE user_id=%s",
                ("2000-01-01T00:00:00+00:00", self.owner_id),
            )
        denied = self.client.put(
            "/api/vault/lifecycle/default",
            headers=self._headers(),
            json={"guided_profile": "ia_after_30"},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"], "reauth_required")

        self._authenticate(self.owner_id, reauth=True)
        with patch("app.main.s3_client", side_effect=RuntimeError("offline")):
            response = self.client.put(
                "/api/vault/lifecycle/default",
                headers=self._headers(),
                json={"guided_profile": "ia_after_30"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["default_policy_id"], body["policies"][0]["id"])
        self.assertEqual(
            body["policies"][0]["profile"]["transitions"][0]["storage_class"],
            "STANDARD_IA",
        )

    def test_owner_can_set_and_clear_folder_override(self) -> None:
        self._authenticate(self.owner_id, reauth=True)
        with patch("app.main.s3_client", side_effect=RuntimeError("offline")):
            created = self.client.put(
                "/api/vault/lifecycle/folder-overrides",
                headers=self._headers(),
                json={
                    "folder_path": "photos",
                    "guided_profile": "archive_tiered",
                    "name": "Photos archive",
                },
            )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["folder_overrides"][0]["folder_path"], "photos")
        self.assertTrue(created.json()["warnings"])

        deleted = self.client.request(
            "DELETE",
            "/api/vault/lifecycle/folder-overrides",
            headers=self._headers(),
            json={"folder_path": "photos"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["folder_overrides"], [])

    def test_custom_default_round_trips_exact_rules_and_preserves_policy_uuid(self) -> None:
        self._authenticate(self.owner_id, reauth=True)
        profile = {
            "transitions": [
                {"days": 30, "storage_class": "STANDARD_IA"},
                {"days": 180, "storage_class": "DEEP_ARCHIVE"},
            ],
            "expiration_days": None,
            "noncurrent_expiration_days": None,
            "noncurrent_transitions": [
                {"days": 90, "storage_class": "GLACIER"},
                {"days": 180, "storage_class": "DEEP_ARCHIVE"},
            ],
        }
        with (
            patch("app.main.s3_client", return_value=object()),
            patch("app.main.sync_lifecycle_rules_for_bucket") as sync,
        ):
            created = self.client.put(
                "/api/vault/lifecycle/default",
                headers=self._headers(),
                json={"profile": profile},
            )
            updated = self.client.put(
                "/api/vault/lifecycle/default",
                headers=self._headers(),
                json={
                    "profile": {
                        **profile,
                        "transitions": [
                            {"days": 30, "storage_class": "STANDARD_IA"},
                            {"days": 120, "storage_class": "GLACIER"},
                        ],
                    }
                },
            )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(
            created.json()["default_policy_id"], updated.json()["default_policy_id"]
        )
        self.assertEqual(created.json()["policies"][0]["profile"], profile)
        self.assertEqual(
            self.client.get("/api/vault/lifecycle").json()["policies"][0]["profile"],
            updated.json()["policies"][0]["profile"],
        )
        self.assertEqual(sync.call_count, 2)

    def test_invalid_custom_profiles_are_rejected_before_mutation(self) -> None:
        self._authenticate(self.owner_id, reauth=True)
        invalid_profiles = (
            {
                "transitions": [
                    {"days": 90, "storage_class": "GLACIER"},
                    {"days": 60, "storage_class": "DEEP_ARCHIVE"},
                ]
            },
            {
                "transitions": [
                    {"days": 30, "storage_class": "STANDARD_IA"},
                    {"days": 60, "storage_class": "ONEZONE_IA"},
                ]
            },
            {"transitions": [{"days": 7, "storage_class": "STANDARD_IA"}]},
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                response = self.client.put(
                    "/api/vault/lifecycle/default",
                    headers=self._headers(),
                    json={"profile": profile},
                )
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/api/vault/lifecycle").json()["policies"], [])

    def test_custom_folder_override_round_trips_and_invokes_sync(self) -> None:
        self._authenticate(self.owner_id, reauth=True)
        profile = {
            "transitions": [
                {"days": 30, "storage_class": "ONEZONE_IA"},
                {"days": 180, "storage_class": "DEEP_ARCHIVE"},
            ],
            "noncurrent_transitions": [],
        }
        with (
            patch("app.main.s3_client", return_value=object()),
            patch("app.main.sync_lifecycle_rules_for_bucket") as sync,
        ):
            response = self.client.put(
                "/api/vault/lifecycle/folder-overrides",
                headers=self._headers(),
                json={"folder_path": "photos/2024", "profile": profile},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        policy_id = body["folder_overrides"][0]["policy_id"]
        stored = next(policy for policy in body["policies"] if policy["id"] == policy_id)
        self.assertEqual(stored["profile"]["transitions"], profile["transitions"])
        self.assertEqual(stored["profile"]["noncurrent_transitions"], [])
        sync.assert_called_once()
