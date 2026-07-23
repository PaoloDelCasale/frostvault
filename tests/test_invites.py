from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import invites
from app.database import SQLiteConnection
from tests.test_database import run_alembic


ISSUER = "https://issuer.example"


class InviteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database_path = Path(self._tmp.name) / "invites.db"
        migrated = run_alembic(self.database_path)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.admin_id = self._add_user("admin", is_admin=True)
        self.target_id = self._add_user("target")

    def connect(self) -> SQLiteConnection:
        return SQLiteConnection(str(self.database_path))

    def _add_user(self, username: str, *, is_admin: bool = False) -> int:
        with self.connect() as connection:
            return connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, %s, NULL, %s)
                RETURNING id
                """,
                (username, username.title(), is_admin),
            ).fetchone()["id"]

    def _invite_row(self, invite_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM invites WHERE id=%s", (invite_id,)
            ).fetchone()

    def _identities(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM user_identities").fetchall()


class CreateInviteTests(InviteTestBase):
    def test_create_invite_stores_only_the_token_hash(self) -> None:
        with self.connect() as connection:
            token = invites.create_invite(
                connection, target_user_id=self.target_id, created_by=self.admin_id
            )

        self.assertTrue(token)
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM invites").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(
            row["token_hash"], hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        self.assertEqual(row["target_user_id"], self.target_id)
        self.assertEqual(row["created_by"], self.admin_id)
        self.assertIsNone(row["redeemed_at"])
        self.assertGreater(
            datetime.fromisoformat(row["expires_at"]), datetime.now(timezone.utc)
        )

    def test_create_invite_for_unknown_user_is_rejected(self) -> None:
        with self.connect() as connection:
            with self.assertRaises(ValueError):
                invites.create_invite(
                    connection, target_user_id=999999, created_by=self.admin_id
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM invites"
                ).fetchone()["total"],
                0,
            )


class RedeemInviteTests(InviteTestBase):
    def _create(self, ttl_seconds: int = 3600) -> tuple[str, int]:
        with self.connect() as connection:
            token = invites.create_invite(
                connection,
                target_user_id=self.target_id,
                created_by=self.admin_id,
                ttl_seconds=ttl_seconds,
            )
            invite_id = connection.execute(
                "SELECT id FROM invites ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        return token, invite_id

    def test_redeem_binds_identity_and_marks_the_invite(self) -> None:
        _, invite_id = self._create()
        with self.connect() as connection:
            user_id = invites.redeem_invite(
                connection, invite_id=invite_id, issuer=ISSUER, subject="sub-1"
            )
        self.assertEqual(user_id, self.target_id)
        identities = self._identities()
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["user_id"], self.target_id)
        self.assertEqual(identities[0]["issuer"], ISSUER)
        self.assertEqual(identities[0]["subject"], "sub-1")
        row = self._invite_row(invite_id)
        self.assertIsNotNone(row["redeemed_at"])
        self.assertEqual(row["redeemed_issuer"], ISSUER)
        self.assertEqual(row["redeemed_subject"], "sub-1")

    def test_redeem_expired_invite_binds_nothing(self) -> None:
        _, invite_id = self._create(ttl_seconds=1)
        with self.connect() as connection:
            connection.execute(
                "UPDATE invites SET expires_at=%s WHERE id=%s",
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    invite_id,
                ),
            )
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.redeem_invite(
                    connection, invite_id=invite_id, issuer=ISSUER, subject="sub-1"
                )
        self.assertEqual(error.exception.reason, "expired")
        self.assertEqual(self._identities(), [])

    def test_redeem_already_used_invite_is_rejected(self) -> None:
        _, invite_id = self._create()
        with self.connect() as connection:
            invites.redeem_invite(
                connection, invite_id=invite_id, issuer=ISSUER, subject="sub-1"
            )
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.redeem_invite(
                    connection, invite_id=invite_id, issuer=ISSUER, subject="sub-2"
                )
        self.assertEqual(error.exception.reason, "already_redeemed")

    def test_redeem_unknown_invite_is_rejected(self) -> None:
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.redeem_invite(
                    connection, invite_id=424242, issuer=ISSUER, subject="sub-1"
                )
        self.assertEqual(error.exception.reason, "unknown")

    def test_redeem_rejects_an_already_bound_identity(self) -> None:
        other = self._add_user("other")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_identities(user_id, issuer, subject, created_at)
                VALUES (%s, %s, 'sub-1', '2026-07-21T00:00:00+00:00')
                """,
                (other, ISSUER),
            )
        _, invite_id = self._create()
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.redeem_invite(
                    connection, invite_id=invite_id, issuer=ISSUER, subject="sub-1"
                )
        self.assertEqual(error.exception.reason, "identity_taken")
        row = self._invite_row(invite_id)
        self.assertIsNone(row["redeemed_at"])


class ResolveInviteTests(InviteTestBase):
    def _create(self, ttl_seconds: int = 3600) -> str:
        with self.connect() as connection:
            return invites.create_invite(
                connection,
                target_user_id=self.target_id,
                created_by=self.admin_id,
                ttl_seconds=ttl_seconds,
            )

    def test_resolve_returns_the_pending_invite_for_a_valid_token(self) -> None:
        token = self._create()
        with self.connect() as connection:
            invite = invites.resolve_invite(connection, token)
        self.assertEqual(invite["target_user_id"], self.target_id)

    def test_resolve_rejects_an_unknown_token(self) -> None:
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.resolve_invite(connection, "not-a-token")
        self.assertEqual(error.exception.reason, "unknown")

    def test_resolve_rejects_an_expired_token(self) -> None:
        token = self._create()
        with self.connect() as connection:
            connection.execute(
                "UPDATE invites SET expires_at=%s",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
            )
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.resolve_invite(connection, token)
        self.assertEqual(error.exception.reason, "expired")

    def test_resolve_rejects_an_already_redeemed_token(self) -> None:
        token = self._create()
        with self.connect() as connection:
            invite_id = connection.execute(
                "SELECT id FROM invites LIMIT 1"
            ).fetchone()["id"]
            invites.redeem_invite(
                connection, invite_id=invite_id, issuer=ISSUER, subject="sub-1"
            )
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.resolve_invite(connection, token)
        self.assertEqual(error.exception.reason, "already_redeemed")


class LinkIdentityTests(InviteTestBase):
    def test_link_identity_binds_an_identity_to_a_user(self) -> None:
        with self.connect() as connection:
            invites.link_identity(
                connection, user_id=self.target_id, issuer=ISSUER, subject="sub-1"
            )
        identities = self._identities()
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["user_id"], self.target_id)

    def test_link_identity_rejects_a_duplicate_binding(self) -> None:
        with self.connect() as connection:
            invites.link_identity(
                connection, user_id=self.target_id, issuer=ISSUER, subject="sub-1"
            )
        with self.connect() as connection:
            with self.assertRaises(invites.InviteError) as error:
                invites.link_identity(
                    connection, user_id=self.admin_id, issuer=ISSUER, subject="sub-1"
                )
        self.assertEqual(error.exception.reason, "identity_taken")


if __name__ == "__main__":
    unittest.main()
