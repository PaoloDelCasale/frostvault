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
            user_id = connection.execute(
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
                (user_id, self.provider.issuer, self.subject),
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


if __name__ == "__main__":
    unittest.main()
