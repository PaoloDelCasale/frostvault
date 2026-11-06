from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

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

from tests.oidc_fake import FakeOidcProvider
from tests.proxy_stack import uvicorn_proxy_app
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
        with PostgresConnection(str(database_path)) as connection:
            self.user_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash)
                VALUES ('alice', 'Alice', 'hash')
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
            database_dsn=database_dsn(str(database_path)),
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
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT state, nonce FROM oidc_login ORDER BY created_at DESC, state DESC LIMIT 1"
            ).fetchone()

    def _add_vault(self) -> int:
        with PostgresConnection(str(self.database_path)) as connection:
            vault_id = connection.execute(
                """
                INSERT INTO vaults(
                    slug, name, source_root, s3_bucket, s3_prefix) VALUES ('oidc-cache-vault', 'OIDC Cache Vault', '/source', 'bucket',
                          'oidc-cache')
                RETURNING id
                """
            ).fetchone()["id"]
        return vault_id

    def test_login_endpoint_redirects_to_provider_and_persists_state(self) -> None:
        self._use_oidc_client()
        response = self.client.get("/auth/oidc/login")
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["state"][0], self._latest_login()["state"])

    def test_trusted_proxy_https_login_and_callback_use_same_redirect_uri(self) -> None:
        self._use_oidc_client()
        proxy_settings = replace(
            self.test_settings,
            cookie_secure=True,
            allowed_hosts="frostvault.example.com",
            trusted_proxies="172.30.0.2/32",
        )
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"}
        # HTTP transport is intentional: HTTPS must come from Uvicorn, not the
        # TestClient base URL. OIDC I/O uses the existing fake provider transport.
        client = TestClient(
            uvicorn_proxy_app(main.app, "172.30.0.2/32"),
            client=("172.30.0.2", 40000),
            base_url="http://frostvault.example.com",
            follow_redirects=False,
        )
        self.addCleanup(client.close)
        with patch.object(main, "settings", proxy_settings):
            started = client.get("/auth/oidc/login", headers=headers)
            self.assertEqual(started.status_code, 303, started.text)
            query = parse_qs(urlparse(started.headers["location"]).query)
            expected_uri = "https://frostvault.example.com/auth/oidc/callback"
            self.assertEqual(query["redirect_uri"], [expected_uri])
            pending = self._latest_login()
            self.assertEqual(query["state"], [pending["state"]])
            self._use_oidc_client(id_token=self.provider.make_id_token(
                nonce=pending["nonce"], subject=self.subject,
            ))
            callback = client.get(
                "/auth/oidc/callback",
                headers=headers,
                params={"state": pending["state"], "code": "auth-code"},
            )
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(self.provider.token_requests[-1]["redirect_uri"], expected_uri)
        self.assertTrue(client.cookies.get(self.cookie_name))
        self.assertIn("Secure", callback.headers["set-cookie"])
        with PostgresConnection(str(self.database_path)) as connection:
            session = connection.execute("SELECT ip FROM sessions").fetchone()
        self.assertEqual(session["ip"], "203.0.113.7")

    def test_untrusted_peer_cannot_force_https_oidc_redirect(self) -> None:
        self._use_oidc_client()
        proxy_settings = replace(
            self.test_settings,
            cookie_secure=True,
            allowed_hosts="frostvault.example.com",
            trusted_proxies="172.30.0.2/32",
        )
        client = TestClient(
            uvicorn_proxy_app(main.app, "172.30.0.2/32"),
            client=("203.0.113.7", 40000),
            base_url="http://frostvault.example.com",
            follow_redirects=False,
        )
        self.addCleanup(client.close)
        with patch.object(main, "settings", proxy_settings):
            response = client.get("/auth/oidc/login", headers={
                "X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1",
            })
        self.assertEqual(response.status_code, 303, response.text)
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.assertEqual(query["redirect_uri"],
                         ["http://frostvault.example.com/auth/oidc/callback"])
        self.assertEqual(query["state"], [self._latest_login()["state"]])

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

    def _complete_login(self, subject: str):
        self._use_oidc_client()
        self.client.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(nonce=pending["nonce"], subject=subject)
        self._use_oidc_client(id_token=id_token)
        return self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )

    def test_first_login_binds_the_owner_identity(self) -> None:
        with PostgresConnection(str(self.database_path)) as connection:
            connection.execute("DELETE FROM user_identities")

        first = self._complete_login("first-subject")
        self.assertEqual(first.status_code, 303, first.text)
        self.assertEqual(self.client.get("/api/me").json()["username"], "alice")
        with PostgresConnection(str(self.database_path)) as connection:
            identities = connection.execute(
                "SELECT user_id, issuer, subject FROM user_identities"
            ).fetchall()
        self.assertEqual(
            identities,
            [
                {
                    "user_id": self.user_id,
                    "issuer": self.provider.issuer,
                    "subject": "first-subject",
                }
            ],
        )

        self.client.cookies.clear()
        other = self._complete_login("second-subject")
        self.assertEqual(other.status_code, 403, other.text)
        self.assertIsNone(self.client.cookies.get(self.cookie_name))

    def test_callback_refuses_another_subject_of_the_bound_issuer(self) -> None:
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
        with PostgresConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT reauth_at FROM sessions ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()["reauth_at"]

    def test_step_up_reauth_refreshes_the_reauth_window(self) -> None:
        self._sign_in()
        with PostgresConnection(str(self.database_path)) as connection:
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

    def test_oidc_step_up_rotates_token_without_offline_cache_authorization(self) -> None:
        self._sign_in()
        self._add_vault()
        before = self.client.get("/api/me")
        self.assertEqual(before.status_code, 200, before.text)
        old_token = self.client.cookies.get(self.cookie_name)
        self.assertNotIn("offline_cache_generation", before.json())

        allowed = self.client.get("/api/files")
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
        self.assertNotIn("offline_cache_generation", after.json())
        allowed = self.client.get("/api/files")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.headers["cache-control"], "no-store")

        stale = self.client.get(
            "/api/files", headers={"Cookie": f"{self.cookie_name}={old_token}"},
        )
        self.assertEqual(stale.status_code, 401, stale.text)

    def test_bug_011_foreign_identity_does_not_replace_the_owner_session(self) -> None:
        """[BUG-011][Req: REQ-023] a foreign Identity never replaces the Session.

        While the owner is signed in, completing OIDC with another subject of
        the same issuer returns 403 and preserves the owner's Session.
        """
        self._sign_in()
        me_before = self.client.get("/api/me")
        self.assertEqual(me_before.status_code, 200, me_before.text)
        owner_cookie = self.client.cookies.get(self.cookie_name)
        self.assertTrue(owner_cookie)

        callback = self._complete_login("subject-foreign")
        self.assertEqual(callback.status_code, 403, callback.text)

        me_after = self.client.get("/api/me")
        self.assertEqual(me_after.status_code, 200, me_after.text)
        self.assertEqual(me_after.json()["username"], "alice")
        self.assertEqual(self.client.cookies.get(self.cookie_name), owner_cookie)

if __name__ == "__main__":
    unittest.main()
