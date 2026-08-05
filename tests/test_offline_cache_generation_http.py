"""Server-side cache-generation guards for issue #192."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.catalog import ArchiveCatalog
from app.config import settings
from app.database import SQLiteConnection
from app.main import OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER, app
from app.security import hash_password
from app.sessions import (
    create_session,
    current_offline_cache_generation,
    offline_cache_generation,
    resolve_session,
    set_session_vault,
)
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

    def _session_row(self) -> dict[str, Any]:
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT offline_cache_generation, offline_cache_nonce, revoked_at,
                       absolute_expires_at, idle_expires_at
                FROM sessions WHERE token_hash=%s
                """,
                (hashlib.sha256(self.first_token.encode("utf-8")).hexdigest(),),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def _select(self, vault_id: int) -> None:
        selected = self.client.post(
            "/api/vaults/select",
            json={"vault_id": vault_id},
            headers={
                "X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""
            },
        )
        self.assertEqual(selected.status_code, 200, selected.text)

    def _second_client(self) -> TestClient:
        client = TestClient(app, client=("127.0.0.1", 50001))
        client.cookies.set(self.test_settings.session_cookie_name, self.first_token)
        csrf = self.client.cookies.get("frostvault_csrf")
        if csrf:
            client.cookies.set(self.test_settings.csrf_cookie_name, csrf)
        return client

    def _listing_that_overlaps(
        self,
        generation: str,
        transition: Callable[[], None],
    ):
        listing_started = Event()
        release_listing = Event()
        original = ArchiveCatalog.list_file_rows

        def blocked_listing(
            catalog: ArchiveCatalog,
            *args: Any,
            **kwargs: Any,
        ):
            listing_started.set()
            if not release_listing.wait(timeout=5):
                raise TimeoutError("test did not release the blocked listing")
            return original(catalog, *args, **kwargs)

        with patch.object(ArchiveCatalog, "list_file_rows", new=blocked_listing):
            with ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(
                    self.client.get,
                    "/api/files",
                    headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation},
                )
                try:
                    self.assertTrue(listing_started.wait(timeout=5))
                    transition()
                finally:
                    release_listing.set()
                return request.result(timeout=5)

    def test_files_reject_old_generations_after_a_to_b_to_a_selection(self) -> None:
        generation_a_first = self._me_generation()
        first_row = self._session_row()

        allowed = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation_a_first},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(
            allowed.headers[OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER],
            generation_a_first,
        )
        self.assertIn(
            OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER,
            allowed.headers["Vary"],
        )

        self._select(self.vault_b)
        generation_b = self._me_generation()
        b_row = self._session_row()

        self._select(self.vault_a)
        generation_a_second = self._me_generation()
        second_row = self._session_row()

        # The generation is a persisted sequence plus a fresh random nonce: it
        # cannot repeat merely because the selected Vault returns to A.
        self.assertTrue(generation_a_first != generation_b)
        self.assertTrue(generation_a_first != generation_a_second)
        self.assertTrue(generation_b != generation_a_second)
        self.assertGreater(
            b_row["offline_cache_generation"],
            first_row["offline_cache_generation"],
        )
        self.assertGreater(
            second_row["offline_cache_generation"],
            b_row["offline_cache_generation"],
        )
        self.assertNotEqual(
            first_row["offline_cache_nonce"],
            second_row["offline_cache_nonce"],
        )

        for stale_generation in (generation_a_first, generation_b):
            stale = self.client.get(
                "/api/files",
                headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: stale_generation},
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(stale.headers.get("Cache-Control"), "no-store")

        current = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation_a_second},
        )
        self.assertEqual(current.status_code, 200, current.text)

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
        self.assertTrue(first_generation != replacement_generation)
        stale = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: first_generation},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_generation_is_persisted_and_monotonic_across_separate_connections(
        self,
    ) -> None:
        with SQLiteConnection(str(self.database_path)) as first_connection:
            first_session = resolve_session(first_connection, self.first_token)
            self.assertIsNotNone(first_session)
            initial_generation = offline_cache_generation(first_session)
            initial_version = first_session["offline_cache_generation"]
            initial_nonce = first_session["offline_cache_nonce"]
            selected_a = set_session_vault(
                first_connection,
                first_session["id"],
                self.vault_a,
                expected_generation=initial_version,
                expected_nonce=initial_nonce,
            )
            self.assertIsNotNone(selected_a)

        with SQLiteConnection(str(self.database_path)) as second_connection:
            current_a = current_offline_cache_generation(
                second_connection,
                first_session["id"],
                self.vault_a,
            )
            self.assertIsNotNone(current_a)
            row_a = second_connection.execute(
                """
                SELECT offline_cache_generation, offline_cache_nonce
                FROM sessions WHERE id=%s
                """,
                (first_session["id"],),
            ).fetchone()
            selected_b = set_session_vault(
                second_connection,
                first_session["id"],
                self.vault_b,
                expected_generation=row_a["offline_cache_generation"],
                expected_nonce=row_a["offline_cache_nonce"],
            )
            self.assertIsNotNone(selected_b)

        with SQLiteConnection(str(self.database_path)) as third_connection:
            current_b = current_offline_cache_generation(
                third_connection,
                first_session["id"],
                self.vault_b,
            )
            final_row = third_connection.execute(
                """
                SELECT offline_cache_generation, offline_cache_nonce
                FROM sessions WHERE id=%s
                """,
                (first_session["id"],),
            ).fetchone()

        self.assertTrue(initial_generation != current_a)
        self.assertTrue(current_a != current_b)
        self.assertGreater(
            final_row["offline_cache_generation"],
            initial_version,
        )
        self.assertGreaterEqual(len(final_row["offline_cache_nonce"]), 32)

    def test_listing_rechecks_persisted_generation_after_concurrent_logout(self) -> None:
        generation = self._me_generation()
        before = self._session_row()

        def logout_from_another_connection() -> None:
            response = self._second_client().post(
                "/api/logout",
                headers={
                    "X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""
                },
            )
            self.assertEqual(response.status_code, 200, response.text)

        response = self._listing_that_overlaps(generation, logout_from_another_connection)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        after = self._session_row()
        self.assertIsNotNone(after["revoked_at"])
        self.assertGreater(
            after["offline_cache_generation"],
            before["offline_cache_generation"],
        )
        self.assertNotEqual(
            after["offline_cache_nonce"],
            before["offline_cache_nonce"],
        )
    def test_expiry_revokes_and_rotates_before_an_old_listing_can_be_reused(self) -> None:
        generation = self._me_generation()
        before = self._session_row()
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET absolute_expires_at=%s", (expired_at,)
            )

        expired = self.client.get(
            "/api/files",
            headers={OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: generation},
        )
        self.assertEqual(expired.status_code, 401, expired.text)
        after = self._session_row()
        self.assertIsNotNone(after["revoked_at"])
        self.assertGreater(
            after["offline_cache_generation"],
            before["offline_cache_generation"],
        )
        self.assertNotEqual(
            after["offline_cache_nonce"],
            before["offline_cache_nonce"],
        )

    def test_listing_returns_409_when_absolute_expiry_commits_during_listing(self) -> None:
        generation = self._me_generation()

        def expire_from_another_connection() -> None:
            expired_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            with SQLiteConnection(str(self.database_path)) as connection:
                connection.execute(
                    "UPDATE sessions SET absolute_expires_at=%s", (expired_at,)
                )

        response = self._listing_that_overlaps(generation, expire_from_another_connection)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(self._session_row()["revoked_at"])


if __name__ == "__main__":
    unittest.main()
