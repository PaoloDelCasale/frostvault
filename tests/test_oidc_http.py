from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import main, oidc
from app.config import settings
from app.database import SQLiteConnection
from tests.oidc_fake import FakeOidcProvider
from tests.test_database import run_alembic


class OidcHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.provider = FakeOidcProvider()
        self.subject = "subject-123"
        with SQLiteConnection(str(database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('alice', 'Alice', 'hash', TRUE)
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, %s, %s, '2026-07-21T00:00:00+00:00')
                """,
                (self.user_id, self.provider.issuer, self.subject),
            )

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(database_path),
            cookie_secure=False,
            oidc_enabled=True,
            oidc_issuer=self.provider.issuer,
            oidc_client_id=self.provider.client_id,
            oidc_client_secret="top-secret",
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.oidc.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.database_path = database_path
        self.client = TestClient(app=main.app, follow_redirects=False)
        self.cookie_name = self.test_settings.session_cookie_name
        resolver = patch.object(
            main,
            "_oidc_host_addresses",
            lambda _: ["93.184.216.34"],
        )
        resolver.start()
        self.addCleanup(resolver.stop)

    def _use_oidc_client(self, *, id_token: str | None = None) -> None:
        patcher = patch.object(
            main, "_oidc_client", lambda: self.provider.client(id_token=id_token)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _latest_login(self) -> dict:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT state, nonce FROM oidc_login ORDER BY rowid DESC LIMIT 1"
            ).fetchone()

    def _add_vault_membership(self) -> int:
        with SQLiteConnection(str(self.database_path)) as connection:
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix, rclone_remote
                ) VALUES ('oidc-cache-vault', 'OIDC Cache Vault', '/source', 'bucket',
                          'oidc-cache', 'remote')
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO vault_members(vault_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (vault_id, self.user_id),
            )
        return vault_id

    def test_login_endpoint_redirects_to_provider_and_persists_state(self) -> None:
        self._use_oidc_client()
        response = self.client.get("/auth/oidc/login")
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["state"][0], self._latest_login()["state"])

    def test_callback_signs_in_a_linked_identity(self) -> None:
        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=self.subject
        )
        self._use_oidc_client(id_token=id_token)

        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 303)
        self.assertTrue(self.client.cookies.get(self.cookie_name))

        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["username"], "alice")

    def test_callback_refuses_an_unlinked_identity(self) -> None:
        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject="unknown-subject"
        )
        self._use_oidc_client(id_token=id_token)

        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 403)
        self.assertIsNone(self.client.cookies.get(self.cookie_name))

    def test_replaying_a_used_state_is_rejected(self) -> None:
        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=self.subject
        )
        self._use_oidc_client(id_token=id_token)
        self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )

        replay = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(replay.status_code, 400)

    def _sign_in(self) -> None:
        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=self.subject
        )
        self._use_oidc_client(id_token=id_token)
        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 303)

    def _reauth_at(self) -> str | None:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT reauth_at FROM sessions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()["reauth_at"]

    def test_step_up_reauth_refreshes_the_reauth_window(self) -> None:
        self._sign_in()
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s", ("2000-01-01T00:00:00+00:00",)
            )

        self._use_oidc_client()
        step_up = self.client.get("/auth/oidc/reauth")
        self.assertEqual(step_up.status_code, 303)
        self.assertEqual(
            parse_qs(urlparse(step_up.headers["location"]).query)["prompt"],
            ["login"],
        )

        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=self.subject
        )
        self._use_oidc_client(id_token=id_token)
        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )

        self.assertEqual(callback.status_code, 303)
        self.assertGreater(
            datetime.fromisoformat(self._reauth_at()),
            datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

    def test_oidc_step_up_rotates_persisted_cache_authorization(self) -> None:
        self._sign_in()
        self._add_vault_membership()
        before = self.client.get("/api/me")
        self.assertEqual(before.status_code, 200, before.text)
        before_generation = before.json()["offline_cache_generation"]

        allowed = self.client.get(
            "/api/files",
            headers={
                main.OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: before_generation
            },
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

        self._use_oidc_client()
        step_up = self.client.get("/auth/oidc/reauth")
        self.assertEqual(step_up.status_code, 303)
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=self.subject
        )
        self._use_oidc_client(id_token=id_token)
        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 303, callback.text)

        after = self.client.get("/api/me")
        self.assertEqual(after.status_code, 200, after.text)
        after_generation = after.json()["offline_cache_generation"]
        self.assertTrue(before_generation != after_generation)

        stale = self.client.get(
            "/api/files",
            headers={
                main.OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: before_generation
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_bug_011_oidc_rejects_foreign_identity_session_switch(self) -> None:
        """[BUG-011][Req: REQ-023] active Session must not be replaced by foreign Identity.

        While alice is signed in, completing OIDC with an Identity already linked
        to bob must return 403 and preserve alice's Session (ADR-0003 / ADR-0005).
        """
        bob_subject = "subject-bob"
        with SQLiteConnection(str(self.database_path)) as connection:
            bob_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('bob', 'Bob', 'hash', FALSE)
                RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, %s, %s, '2026-07-21T00:00:00+00:00')
                """,
                (bob_id, self.provider.issuer, bob_subject),
            )

        self._sign_in()
        me_before = self.client.get("/api/me")
        self.assertEqual(me_before.status_code, 200, me_before.text)
        self.assertEqual(me_before.json()["username"], "alice")
        alice_cookie = self.client.cookies.get(self.cookie_name)
        self.assertTrue(alice_cookie)

        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject=bob_subject
        )
        self._use_oidc_client(id_token=id_token)

        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 403, callback.text)
        self.assertIn("sign out first", callback.text.lower())

        me_after = self.client.get("/api/me")
        self.assertEqual(me_after.status_code, 200, me_after.text)
        self.assertEqual(me_after.json()["username"], "alice")
        self.assertEqual(self.client.cookies.get(self.cookie_name), alice_cookie)


if __name__ == "__main__":
    unittest.main()
