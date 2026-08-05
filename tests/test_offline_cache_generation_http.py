"""Server-side cache-generation guards for issue #192."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER, app
from app.security import hash_password
from app.sessions import create_session
from tests.test_database import run_alembic


class OfflineCacheGenerationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database_path = Path(self._directory.name) / "offline-cache.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with SQLiteConnection(str(self.database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('owner', 'Owner', %s, FALSE)
                RETURNING id
                """,
                (hash_password("correct horse battery staple"),),
            ).fetchone()["id"]
            self.vault_a = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('vault-a', 'Vault A', '/source-a', 'bucket', 'a', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            self.vault_b = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('vault-b', 'Vault B', '/source-b', 'bucket', 'b', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (self.vault_a, self.user_id),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (%s, %s, 'owner')",
                (self.vault_b, self.user_id),
            )
            self.first_token = create_session(
                connection,
                user_id=self.user_id,
                auth_method="local",
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.client.cookies.set(self.test_settings.session_cookie_name, self.first_token)

    def _me_generation(self) -> str:
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        generation = response.json()["offline_cache_generation"]
        self.assertIsInstance(generation, str)
        self.assertTrue(generation)
        return generation

    def test_files_reject_an_old_generation_after_selected_vault_changes(self) -> None:
        generation_a = self._me_generation()
        allowed = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation_a},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(
            allowed.headers[OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER], generation_a
        )

        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": self.vault_b},
            headers={
                "X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""
            },
        )
        self.assertEqual(selected.status_code, 200, selected.text)

        stale = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation_a},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

        generation_b = self._me_generation()
        self.assertNotEqual(generation_a, generation_b)
        current = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation_b},
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(
            current.headers[OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER], generation_b
        )

    def test_a_replaced_session_gets_a_different_generation_for_the_same_vault(self) -> None:
        first_generation = self._me_generation()
        with SQLiteConnection(str(self.database_path)) as connection:
            replacement = create_session(
                connection,
                user_id=self.user_id,
                auth_method="local",
            )
        self.client.cookies.set(self.test_settings.session_cookie_name, replacement)

        replacement_generation = self._me_generation()
        self.assertNotEqual(first_generation, replacement_generation)
        stale = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: first_generation},
        )
        self.assertEqual(stale.status_code, 409, stale.text)


if __name__ == "__main__":
    unittest.main()
