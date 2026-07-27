"""Global User and Identity administration APIs (issue #135).

Exercises the public admin HTTP boundary: role changes, authentication
capabilities, linked Identity inspection and Invite administration.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SQLiteConnection
from app.main import app
from app.security import hash_password
from app.sessions import create_session
from tests.test_database import run_alembic


ADMIN_PASSWORD = "correct horse battery"


class AdminIdentityTestCase(unittest.TestCase):
    """Migrated SQLite database with one administrator and one member."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "app.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        with self.connect() as connection:
            self.admin_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('alice', 'Alice', %s, TRUE)
                RETURNING id
                """,
                (hash_password(ADMIN_PASSWORD),),
            ).fetchone()["id"]
            self.member_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('bob', 'Bob', %s, FALSE)
                RETURNING id
                """,
                (hash_password(ADMIN_PASSWORD),),
            ).fetchone()["id"]

        self.settings = replace(
            settings,
            db_backend="sqlite",
            sqlite_path=str(self.database_path),
            cookie_secure=False,
        )
        for target in (
            "app.main.settings",
            "app.database.settings",
            "app.sessions.settings",
            "app.invites.settings",
        ):
            patcher = patch(target, self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

    def sign_in_as_admin(self) -> None:
        response = self.client.post(
            "/api/login", json={"username": "alice", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def sign_in_as_member(self) -> None:
        # Non-admins cannot use Break-glass Login; seed the server-side Session
        # they would obtain through OIDC.
        with self.connect() as connection:
            raw_token = create_session(
                connection, user_id=self.member_id, auth_method="oidc"
            )
        self.client.cookies.set(self.settings.session_cookie_name, raw_token)

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.get("/api/me").json()["csrf_token"]}


class AdminRoleAdministrationTests(AdminIdentityTestCase):
    def test_administrator_promotes_a_user(self) -> None:
        self.sign_in_as_admin()

        response = self.client.patch(
            f"/api/admin/users/{self.member_id}",
            headers=self.headers(),
            json={"is_admin": True},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_admin"])

    def test_administrator_demotes_another_administrator(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET is_admin=TRUE WHERE id=%s", (self.member_id,)
            )
        self.sign_in_as_admin()

        response = self.client.patch(
            f"/api/admin/users/{self.member_id}",
            headers=self.headers(),
            json={"is_admin": False},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_admin"])

    def test_administrator_cannot_demote_themselves(self) -> None:
        self.sign_in_as_admin()

        response = self.client.patch(
            f"/api/admin/users/{self.admin_id}",
            headers=self.headers(),
            json={"is_admin": False},
        )

        self.assertEqual(response.status_code, 400, response.text)
        with self.connect() as connection:
            still_admin = connection.execute(
                "SELECT is_admin FROM users WHERE id=%s", (self.admin_id,)
            ).fetchone()
        self.assertTrue(still_admin["is_admin"])

    def test_promotion_is_audited(self) -> None:
        self.sign_in_as_admin()

        self.client.patch(
            f"/api/admin/users/{self.member_id}",
            headers=self.headers(),
            json={"is_admin": True},
        )

        events = self.client.get("/api/admin/audit-events").json()["events"]
        changes = [
            event for event in events if event["event"] == "admin_user_role_changed"
        ]
        self.assertEqual(len(changes), 1, events)
        self.assertEqual(changes[0]["actor_user_id"], self.admin_id)
        self.assertEqual(changes[0]["detail"]["target_user_id"], self.member_id)
        self.assertTrue(changes[0]["detail"]["is_admin"])


class AdminUserCapabilityTests(AdminIdentityTestCase):
    def test_user_list_reports_authentication_capabilities(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, 'https://idp.example', 'bob-subject', '2026-07-01')
                """,
                (self.member_id,),
            )
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('dana', 'Dana', NULL, FALSE)
                """
            )
        self.sign_in_as_admin()

        response = self.client.get("/api/admin/users")

        self.assertEqual(response.status_code, 200, response.text)
        users = {item["username"]: item for item in response.json()["items"]}
        self.assertTrue(users["bob"]["has_password"])
        self.assertEqual(users["bob"]["identity_count"], 1)
        self.assertFalse(users["dana"]["has_password"])
        self.assertEqual(users["dana"]["identity_count"], 0)
        self.assertNotIn("password_hash", users["bob"])

    def test_creating_a_passwordless_user_reports_no_password(self) -> None:
        self.sign_in_as_admin()

        response = self.client.post(
            "/api/admin/users",
            headers=self.headers(),
            json={"username": "erin", "display_name": "Erin", "password": None},
        )

        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertFalse(created["has_password"])
        self.assertEqual(created["identity_count"], 0)
        self.assertNotIn("password_hash", created)

    def test_creating_a_local_user_reports_a_configured_password(self) -> None:
        self.sign_in_as_admin()

        response = self.client.post(
            "/api/admin/users",
            headers=self.headers(),
            json={
                "username": "frank",
                "display_name": "Frank",
                "password": "another horse battery",
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["has_password"])

    def test_user_creation_is_audited_without_credentials(self) -> None:
        self.sign_in_as_admin()

        created = self.client.post(
            "/api/admin/users",
            headers=self.headers(),
            json={
                "username": "grace",
                "display_name": "Grace",
                "password": "correct horse battery",
            },
        ).json()

        events = self.client.get("/api/admin/audit-events").json()["events"]
        creations = [event for event in events if event["event"] == "admin_user_created"]
        self.assertEqual(len(creations), 1, events)
        self.assertEqual(creations[0]["actor_user_id"], self.admin_id)
        self.assertEqual(creations[0]["detail"]["target_user_id"], created["id"])
        self.assertTrue(creations[0]["detail"]["has_password"])
        self.assertNotIn("correct horse battery", str(creations[0]["detail"]))


class AdminIdentityInspectionTests(AdminIdentityTestCase):
    def link_identity(self, user_id: int, subject: str) -> int:
        with self.connect() as connection:
            return connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, 'https://idp.example', %s, '2026-07-01T00:00:00+00:00')
                RETURNING id
                """,
                (user_id, subject),
            ).fetchone()["id"]

    def test_linked_identities_are_listed_by_issuer_and_subject(self) -> None:
        identity_id = self.link_identity(self.member_id, "bob-subject")
        self.sign_in_as_admin()

        response = self.client.get(f"/api/admin/users/{self.member_id}/identities")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["items"],
            [
                {
                    "id": identity_id,
                    "issuer": "https://idp.example",
                    "subject": "bob-subject",
                    "created_at": "2026-07-01T00:00:00+00:00",
                }
            ],
        )

    def test_identities_of_an_unknown_user_are_refused(self) -> None:
        self.sign_in_as_admin()

        response = self.client.get("/api/admin/users/9999/identities")

        self.assertEqual(response.status_code, 404, response.text)


class AdminIdentityUnlinkTests(AdminIdentityInspectionTests):
    def test_confirmed_unlink_removes_the_identity_and_audits_it(self) -> None:
        identity_id = self.link_identity(self.member_id, "bob-subject")
        self.sign_in_as_admin()

        response = self.client.delete(
            f"/api/admin/users/{self.member_id}/identities/{identity_id}"
            "?confirm=true",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])
        with self.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM user_identities"
            ).fetchone()["total"]
        self.assertEqual(remaining, 0)

        events = self.client.get("/api/admin/audit-events").json()["events"]
        unlinked = [
            event for event in events if event["event"] == "admin_identity_unlinked"
        ]
        self.assertEqual(len(unlinked), 1, events)
        self.assertEqual(unlinked[0]["detail"]["subject"], "bob-subject")
        self.assertEqual(unlinked[0]["detail"]["issuer"], "https://idp.example")
        self.assertEqual(unlinked[0]["actor_user_id"], self.admin_id)

    def test_unlink_without_confirmation_is_refused(self) -> None:
        identity_id = self.link_identity(self.member_id, "bob-subject")
        self.sign_in_as_admin()

        response = self.client.delete(
            f"/api/admin/users/{self.member_id}/identities/{identity_id}",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 400, response.text)
        with self.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM user_identities"
            ).fetchone()["total"]
        self.assertEqual(remaining, 1)

    def test_unlinking_the_only_sign_in_method_is_refused(self) -> None:
        with self.connect() as connection:
            passwordless_id = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES ('dana', 'Dana', NULL, FALSE)
                RETURNING id
                """
            ).fetchone()["id"]
        identity_id = self.link_identity(passwordless_id, "dana-subject")
        self.sign_in_as_admin()

        response = self.client.delete(
            f"/api/admin/users/{passwordless_id}/identities/{identity_id}"
            "?confirm=true",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 409, response.text)
        with self.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM user_identities WHERE user_id=%s",
                (passwordless_id,),
            ).fetchone()["total"]
        self.assertEqual(remaining, 1)

    def test_unlink_requires_recent_reauthentication(self) -> None:
        identity_id = self.link_identity(self.member_id, "bob-subject")
        self.sign_in_as_admin()
        headers = self.headers()
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET reauth_at=%s", ("2000-01-01T00:00:00+00:00",)
            )

        response = self.client.delete(
            f"/api/admin/users/{self.member_id}/identities/{identity_id}"
            "?confirm=true",
            headers=headers,
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})


class AdminInviteAdministrationTests(AdminIdentityTestCase):
    def create_invite(self) -> str:
        response = self.client.post(
            "/api/admin/invites",
            headers=self.headers(),
            json={"target_user_id": self.member_id},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["token"]

    def test_pending_invites_are_listed_without_token_material(self) -> None:
        self.sign_in_as_admin()
        token = self.create_invite()

        response = self.client.get("/api/admin/invites")

        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1, items)
        invite = items[0]
        self.assertEqual(invite["target_user_id"], self.member_id)
        self.assertEqual(invite["target_username"], "bob")
        self.assertEqual(invite["created_by"], self.admin_id)
        self.assertIn("expires_at", invite)
        self.assertNotIn("token", invite)
        self.assertNotIn("token_hash", invite)
        self.assertNotIn(token, response.text)

    def test_redeemed_and_expired_invites_are_not_pending(self) -> None:
        self.sign_in_as_admin()
        self.create_invite()
        self.create_invite()
        with self.connect() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM invites ORDER BY id"
                ).fetchall()
            ]
            connection.execute(
                "UPDATE invites SET redeemed_at='2026-07-02T00:00:00+00:00' "
                "WHERE id=%s",
                (ids[0],),
            )
            connection.execute(
                "UPDATE invites SET expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE id=%s",
                (ids[1],),
            )

        response = self.client.get("/api/admin/invites")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])

    def test_revoked_invite_is_no_longer_pending_and_is_audited(self) -> None:
        self.sign_in_as_admin()
        token = self.create_invite()
        with self.connect() as connection:
            invite_id = connection.execute(
                "SELECT id FROM invites ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

        response = self.client.post(
            f"/api/admin/invites/{invite_id}/revoke", headers=self.headers()
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(token, response.text)
        self.assertNotIn("token_hash", response.text)
        self.assertEqual(self.client.get("/api/admin/invites").json()["items"], [])

        events = self.client.get("/api/admin/audit-events").json()["events"]
        revoked = [
            event for event in events if event["event"] == "admin_invite_revoked"
        ]
        self.assertEqual(len(revoked), 1, events)
        self.assertEqual(revoked[0]["actor_user_id"], self.admin_id)
        self.assertEqual(revoked[0]["detail"]["invite_id"], invite_id)
        self.assertNotIn(token, str(revoked[0]["detail"]))

    def test_revoking_an_unknown_invite_is_refused(self) -> None:
        self.sign_in_as_admin()

        response = self.client.post(
            "/api/admin/invites/9999/revoke", headers=self.headers()
        )

        self.assertEqual(response.status_code, 404, response.text)


class AdminOnlyAccessTests(AdminIdentityTestCase):
    """Every new administration endpoint is closed to non-administrators."""

    def test_member_cannot_read_or_change_identities_and_invites(self) -> None:
        self.sign_in_as_member()
        headers = self.headers()

        refused = {
            "list identities": self.client.get(
                f"/api/admin/users/{self.admin_id}/identities"
            ),
            "unlink identity": self.client.delete(
                f"/api/admin/users/{self.admin_id}/identities/1?confirm=true",
                headers=headers,
            ),
            "list invites": self.client.get("/api/admin/invites"),
            "revoke invite": self.client.post(
                "/api/admin/invites/1/revoke", headers=headers
            ),
            "promote user": self.client.patch(
                f"/api/admin/users/{self.member_id}",
                headers=headers,
                json={"is_admin": True},
            ),
        }

        for label, response in refused.items():
            with self.subTest(label):
                self.assertEqual(response.status_code, 403, response.text)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT is_admin FROM users WHERE id=%s", (self.member_id,)
            ).fetchone()
        self.assertFalse(row["is_admin"])

    def test_signed_out_callers_are_refused(self) -> None:
        for response in (
            self.client.get(f"/api/admin/users/{self.admin_id}/identities"),
            self.client.get("/api/admin/invites"),
        ):
            self.assertEqual(response.status_code, 401, response.text)


class AdminReauthenticationTests(AdminIdentityTestCase):
    """Invite revocation is a sensitive mutation and needs recent Reauthentication."""

    def test_revoking_an_invite_requires_recent_reauthentication(self) -> None:
        self.sign_in_as_admin()
        created = self.client.post(
            "/api/admin/invites",
            headers=self.headers(),
            json={"target_user_id": self.member_id},
        )
        self.assertEqual(created.status_code, 201, created.text)
        headers = self.headers()
        with self.connect() as connection:
            invite_id = connection.execute(
                "SELECT id FROM invites ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE sessions SET reauth_at=%s", ("2000-01-01T00:00:00+00:00",)
            )

        response = self.client.post(
            f"/api/admin/invites/{invite_id}/revoke", headers=headers
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "reauth_required"})
        self.assertEqual(len(self.client.get("/api/admin/invites").json()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
