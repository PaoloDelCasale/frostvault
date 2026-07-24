from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.database import SQLiteConnection
from app.security import hash_password
from app.sessions import create_session, csrf_token_for
from tests.oidc_fake import FakeOidcProvider
from tests.test_database import run_alembic


ADMIN_PASSWORD = "correct-horse-battery"


class InviteHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        self.provider = FakeOidcProvider()
        with SQLiteConnection(str(self.database_path)) as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('admin', 'Admin', %s, TRUE)
                RETURNING id
                """,
                (hash_password(ADMIN_PASSWORD),),
            ).fetchone()["id"]
            self.member_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('member', 'Member', %s, FALSE)
                RETURNING id
                """,
                (hash_password(ADMIN_PASSWORD),),
            ).fetchone()["id"]

        self.test_settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
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
            "app.invites.settings",
        ):
            patcher = patch(target, self.test_settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app=main.app, client=("127.0.0.1", 50000), follow_redirects=False)
        self.cookie_name = self.test_settings.session_cookie_name

    def _use_oidc_client(self, *, id_token: str | None = None) -> None:
        patcher = patch.object(
            main, "_oidc_client", lambda: self.provider.client(id_token=id_token)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sign_in(self, username: str) -> None:
        response = self.client.post(
            "/api/login", json={"username": username, "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _authenticate_session(self, user_id: int) -> None:
        # Non-admins cannot use Break-glass Login, so give them the kind of
        # server-side Session they would obtain through OIDC.
        with SQLiteConnection(str(self.database_path)) as connection:
            raw_token = create_session(
                connection, user_id=user_id, auth_method="oidc"
            )
            csrf_token = csrf_token_for(connection, raw_token)
        self.client.cookies.set(self.cookie_name, raw_token)
        self.client.cookies.set("frostvault_csrf", csrf_token)

    def _csrf(self) -> dict:
        return {"X-CSRF-Token": self.client.cookies.get("frostvault_csrf") or ""}

    def _latest_login(self) -> dict:
        with SQLiteConnection(str(self.database_path)) as connection:
            return connection.execute(
                "SELECT state, nonce, invite_id FROM oidc_login "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()

    # --- admin invite creation -------------------------------------------

    def test_admin_creates_an_invite_for_a_target_user(self) -> None:
        self._sign_in("admin")
        response = self.client.post(
            "/api/admin/invites",
            json={"target_user_id": self.member_id},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["token"])

    def test_non_admin_cannot_create_invites(self) -> None:
        self._authenticate_session(self.member_id)
        response = self.client.post(
            "/api/admin/invites", json={"target_user_id": self.admin_id}
        )
        self.assertEqual(response.status_code, 403)

    # --- shell users ------------------------------------------------------

    def test_admin_can_create_a_passwordless_shell_user(self) -> None:
        self._sign_in("admin")
        response = self.client.post(
            "/api/admin/users",
            json={"username": "oidcuser", "display_name": "OIDC User"},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username='oidcuser'"
            ).fetchone()
        self.assertIsNone(row["password_hash"])

    def test_shell_user_cannot_break_glass_login(self) -> None:
        self._sign_in("admin")
        self.client.post(
            "/api/admin/users",
            json={"username": "oidcuser", "display_name": "OIDC User"},
            headers=self._csrf(),
        )
        fresh = TestClient(app=main.app, client=("127.0.0.1", 50000), follow_redirects=False)
        response = fresh.post(
            "/api/login", json={"username": "oidcuser", "password": ""}
        )
        self.assertEqual(response.status_code, 401)

    # --- invite redemption through OIDC ----------------------------------

    def _create_invite(self, target_user_id: int) -> str:
        self._sign_in("admin")
        response = self.client.post(
            "/api/admin/invites",
            json={"target_user_id": target_user_id},
            headers=self._csrf(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        token = response.json()["token"]
        self.client.post("/api/logout", headers=self._csrf())
        return token

    def test_invited_identity_is_bound_and_signed_in(self) -> None:
        token = self._create_invite(self.member_id)
        anon = TestClient(app=main.app, client=("127.0.0.1", 50000), follow_redirects=False)
        self._use_oidc_client()
        anon.get("/auth/oidc/login", params={"invite": token})
        pending = self._latest_login()
        self.assertIsNotNone(pending["invite_id"])
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject="new-subject"
        )
        self._use_oidc_client(id_token=id_token)

        callback = anon.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertTrue(anon.cookies.get(self.cookie_name))
        me = anon.get("/api/me")
        self.assertEqual(me.json()["username"], "member")

        with SQLiteConnection(str(self.database_path)) as connection:
            redeemed = connection.execute(
                "SELECT redeemed_subject FROM invites"
            ).fetchone()
        self.assertEqual(redeemed["redeemed_subject"], "new-subject")

    def test_unlinked_identity_without_invite_is_refused(self) -> None:
        anon = TestClient(app=main.app, client=("127.0.0.1", 50000), follow_redirects=False)
        self._use_oidc_client()
        anon.get("/auth/oidc/login")
        pending = self._latest_login()
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject="stranger"
        )
        self._use_oidc_client(id_token=id_token)
        callback = anon.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 403)
        self.assertIsNone(anon.cookies.get(self.cookie_name))

    def test_expired_invite_is_rejected_at_login(self) -> None:
        token = self._create_invite(self.member_id)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE invites SET expires_at='2000-01-01T00:00:00+00:00'"
            )
        anon = TestClient(app=main.app, client=("127.0.0.1", 50000), follow_redirects=False)
        self._use_oidc_client()
        response = anon.get("/auth/oidc/login", params={"invite": token})
        self.assertEqual(response.status_code, 400)

    # --- self-link --------------------------------------------------------

    def test_authenticated_user_self_links_their_identity(self) -> None:
        self._authenticate_session(self.member_id)
        self._use_oidc_client()
        begin = self.client.get("/auth/oidc/link")
        self.assertEqual(begin.status_code, 303, begin.text)
        pending = self._latest_login()
        self.assertIsNotNone(pending["invite_id"])
        id_token = self.provider.make_id_token(
            nonce=pending["nonce"], subject="member-subject"
        )
        self._use_oidc_client(id_token=id_token)
        callback = self.client.get(
            "/auth/oidc/callback",
            params={"state": pending["state"], "code": "auth-code"},
        )
        self.assertEqual(callback.status_code, 303, callback.text)
        with SQLiteConnection(str(self.database_path)) as connection:
            identity = connection.execute(
                "SELECT user_id FROM user_identities WHERE subject='member-subject'"
            ).fetchone()
        self.assertEqual(identity["user_id"], self.member_id)

    def test_bug_021_oidc_link_requires_recent_reauth(self) -> None:
        """[BUG-021][Req: REQ-033] self-link must gate invite creation on reauth.

        A stale Session must not mint a self-Invite (ADR-0005 / ADR-0003). With
        recent reauth, begin_login must request a fresh IdP login via prompt=login.
        """
        self._authenticate_session(self.member_id)
        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s",
                ("2000-01-01T00:00:00+00:00",),
            )
            invites_before = connection.execute(
                "SELECT COUNT(*) AS n FROM invites"
            ).fetchone()["n"]

        self._use_oidc_client()
        denied = self.client.get("/auth/oidc/link")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json(), {"error": "reauth_required"})

        with SQLiteConnection(str(self.database_path)) as connection:
            invites_after = connection.execute(
                "SELECT COUNT(*) AS n FROM invites"
            ).fetchone()["n"]
        self.assertEqual(invites_after, invites_before)

        with SQLiteConnection(str(self.database_path)) as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s",
                ("2099-01-01T00:00:00+00:00",),
            )
        allowed = self.client.get("/auth/oidc/link")
        self.assertEqual(allowed.status_code, 303, allowed.text)
        self.assertEqual(
            parse_qs(urlparse(allowed.headers["location"]).query).get("prompt"),
            ["login"],
        )


if __name__ == "__main__":
    unittest.main()
